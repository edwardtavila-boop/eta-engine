"""
EVOLUTIONARY TRADING ALGO  //  chaos.scheduler
==============================================
Cadence + lockout helper for chaos drills.

The avengers daemon runs ``ChaosScheduler.due_drills(now)`` once per
tick. Drills that are due fire (in dry-run mode by default) and the
last-fired timestamp is persisted to
``~/.local/state/eta_engine/chaos_state.json``.

Design constraints
------------------
* **Never run during a US session** (13:30 - 21:00 UTC). The
  scheduler refuses to fire any drill in that window even if otherwise
  due.
* **One drill at a time.** Two simultaneous drills would tangle the
  observation and waste the value of the exercise.
* **Persist timestamps via atomic-replace** so a crash mid-run doesn't
  duplicate the entry on restart.

Public API
----------

* :class:`ChaosScheduleEntry` -- ``(drill_name, every_days)``.
* :class:`ChaosScheduler`     -- ``due_drills(now)`` and ``mark_run(drill, ts)``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = (
    Path("~/.local/state/eta_engine/chaos_state.json").expanduser()
)

# US trading session in UTC (NYSE: 13:30 - 20:00 UTC, plus 1h overlap
# for European close + GLOBEX close-out volatility).
SESSION_BLACKOUT_START = dtime(13, 0)
SESSION_BLACKOUT_END   = dtime(21, 0)


@dataclass(frozen=True)
class ChaosScheduleEntry:
    drill_name: str
    every_days: float
    severity_max: str = "medium"   # don't auto-run "high" drills


@dataclass
class ChaosScheduler:
    schedule:    list[ChaosScheduleEntry] = field(default_factory=list)
    state_path:  Path = DEFAULT_STATE_PATH
    last_run:    dict[str, str] = field(default_factory=dict)  # name -> ISO ts

    def __post_init__(self) -> None:
        self._load()

    # ------------------------------------------------------------------
    # State I/O
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("ChaosScheduler: state load failed: %s", e)
            return
        if isinstance(payload, dict):
            for k, v in payload.items():
                if isinstance(v, str):
                    self.last_run[k] = v

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.state_path.name + ".",
                                   dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.last_run, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def due_drills(self, now: datetime | None = None) -> list[ChaosScheduleEntry]:
        """Return entries that are due AND outside the session blackout."""
        n = now or datetime.now(UTC)
        # Hard rule: never fire during the session window.
        if SESSION_BLACKOUT_START <= n.time() <= SESSION_BLACKOUT_END:
            return []
        out: list[ChaosScheduleEntry] = []
        for entry in self.schedule:
            last_iso = self.last_run.get(entry.drill_name, "")
            if not last_iso:
                out.append(entry)
                continue
            try:
                last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            except ValueError:
                out.append(entry)
                continue
            elapsed = (n - last).total_seconds()
            if elapsed >= entry.every_days * 86_400:
                out.append(entry)
        # Cap to one per tick (constraint above).
        return out[:1]

    def mark_run(
        self,
        drill_name: str,
        ts: datetime | None = None,
    ) -> None:
        """Record that a drill ran (regardless of outcome) and persist."""
        n = ts or datetime.now(UTC)
        self.last_run[drill_name] = n.isoformat().replace("+00:00", "Z")
        self._save()

    def in_session_blackout(self, now: datetime | None = None) -> bool:
        n = now or datetime.now(UTC)
        return SESSION_BLACKOUT_START <= n.time() <= SESSION_BLACKOUT_END


def make_default_schedule() -> list[ChaosScheduleEntry]:
    """Reasonable monthly cadence for the built-in drills.

    Severity-high drills are NOT in the default schedule; the
    operator opts them in explicitly.
    """
    return [
        ChaosScheduleEntry("chrony_kill",           every_days=30, severity_max="low"),
        ChaosScheduleEntry("redis_stall",           every_days=30, severity_max="low"),
        ChaosScheduleEntry("ws_disconnect_bybit",   every_days=45, severity_max="medium"),
        ChaosScheduleEntry("dns_jam",               every_days=45, severity_max="medium"),
    ]


# Optional helper: sequence of due drills for a specific tick.
def due_for_tick(
    scheduler: ChaosScheduler,
    now: datetime | None = None,
) -> Sequence[ChaosScheduleEntry]:
    return scheduler.due_drills(now)

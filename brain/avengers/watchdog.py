"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.watchdog
==========================================
Sibling-daemon healer. Reads heartbeats out of the Avengers journal,
detects stuck / dead daemons, alerts the operator, and (optionally)
relaunches them via a persona -> .bat mapping.

Why this exists
---------------
Four daemons run 24/7 on the VPS. Any of them can die silently:

  * Windows Update reboot flips the machine but ``FLEET.lnk`` in Startup
    might fail to re-launch if something is slow to come up.
  * A Python exception before the main loop returns is caught but a
    second-stage exception could kill the process.
  * A network hiccup wedges a subprocess.

We want each daemon to babysit the others. The JARVIS daemon schedules
a cheap watchdog sweep every 3 minutes, Batman every 7, etc. -- so
no single point of failure. First one to notice a sibling is down fires
a push alert and attempts a cold restart.

Design
------
1. Each ``tick`` in ``daemon.py`` appends a heartbeat record with
   ``kind="heartbeat"``, ``persona=<self>``, ``ts=<now>``.
2. The Watchdog tails the last 60 min of journal, groups by persona,
   takes max(ts).
3. Classifies: HEALTHY (< stuck_minutes), STUCK (>= stuck_minutes,
   < offline_minutes), OFFLINE (>= offline_minutes, or never seen).
4. For each STUCK / OFFLINE persona: send a push alert. If a
   ``WatchdogRelauncher`` was wired up and the persona is OFFLINE, try
   to cold-start the .bat.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.base import AVENGERS_JOURNAL
from eta_engine.brain.avengers.push import AlertLevel, PushBus, default_bus

# All four personas in the fleet. Kept as strings (not PersonaId) so the
# watchdog can survive an import cycle if base.py is being changed.
FLEET_PERSONAS: tuple[str, ...] = ("JARVIS", "BATMAN", "ALFRED", "ROBIN")


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    STUCK = "STUCK"
    OFFLINE = "OFFLINE"


class DaemonHealth(BaseModel):
    """One persona's health snapshot."""

    model_config = ConfigDict(frozen=True)

    persona: str
    status: HealthStatus
    last_heartbeat: datetime | None
    minutes_since: float = Field(ge=0.0)
    note: str = ""


class WatchdogReport(BaseModel):
    """Full sweep output. Persist / render as-is."""

    model_config = ConfigDict(frozen=True)

    checked_at: datetime
    daemons: list[DaemonHealth]
    alerts_fired: list[str] = Field(default_factory=list)
    relaunches: list[str] = Field(default_factory=list)


class WatchdogRelauncher:
    """Maps a persona to a .bat (or command) that cold-starts its daemon.

    Parameters
    ----------
    launcher_dir
        Directory containing ``daemon_<persona>.bat``. Defaults to
        ``Desktop/Base/launchers``.
    extra_mapping
        Optional per-persona override: ``{"JARVIS": Path(...)}``.
    """

    def __init__(
        self,
        launcher_dir: Path | None = None,
        *,
        extra_mapping: dict[str, Path] | None = None,
    ) -> None:
        if launcher_dir is None:
            # Project default -- matches what install_desktop_shortcuts.ps1 writes.
            # post-OneDrive-migration 2026-04-26: launchers live under C:\EvolutionaryTradingAlgo\.
            launcher_dir = Path("C:/EvolutionaryTradingAlgo/launchers")
        self.launcher_dir = launcher_dir
        self._mapping: dict[str, Path] = {}
        for persona in FLEET_PERSONAS:
            bat = launcher_dir / f"daemon_{persona.lower()}.bat"
            self._mapping[persona] = bat
        if extra_mapping:
            self._mapping.update(extra_mapping)

    def launcher_for(self, persona: str) -> Path | None:
        return self._mapping.get(persona.upper())

    def relaunch(self, persona: str) -> bool:
        """Fire-and-forget cold start. Returns True if we actually spawned."""
        bat = self.launcher_for(persona)
        if bat is None or not bat.exists():
            return False
        try:
            # Detached on Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            flags = 0
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags |= subprocess.DETACHED_PROCESS
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                ["cmd", "/c", str(bat)],
                creationflags=flags,
                close_fds=True,
            )
        except OSError:
            return False
        return True


class Watchdog:
    """Sweeps the journal and alerts / heals as needed.

    Parameters
    ----------
    journal_path
        JSONL file to tail. Defaults to the Avengers journal.
    push_bus
        Where to send alerts. Defaults to the module-level ``default_bus``.
    stuck_minutes
        If last heartbeat older than this -> STUCK (warn).
    offline_minutes
        If last heartbeat older than this -> OFFLINE (critical + relaunch).
    lookback_minutes
        How far back to read the journal. Must be > offline_minutes.
    relauncher
        Optional. Pass ``WatchdogRelauncher()`` to enable cold-restart.
        Leave None to make the watchdog strictly observational.
    """

    def __init__(
        self,
        *,
        journal_path: Path | None = None,
        push_bus: PushBus | None = None,
        stuck_minutes: float = 5.0,
        offline_minutes: float = 15.0,
        lookback_minutes: float = 60.0,
        relauncher: WatchdogRelauncher | None = None,
        clock: callable | None = None,
        self_persona: str | None = None,
    ) -> None:
        if not (0 < stuck_minutes < offline_minutes <= lookback_minutes):
            msg = "watchdog thresholds must satisfy 0 < stuck < offline <= lookback"
            raise ValueError(msg)
        self.journal_path = journal_path or AVENGERS_JOURNAL
        self.push_bus = push_bus or default_bus()
        self.stuck_minutes = stuck_minutes
        self.offline_minutes = offline_minutes
        self.lookback_minutes = lookback_minutes
        self.relauncher = relauncher
        self._clock = clock or (lambda: datetime.now(UTC))
        self.self_persona = (self_persona or "").upper() or None

    def _read_heartbeats(self) -> dict[str, datetime]:
        """Return latest heartbeat timestamp per persona."""
        if not self.journal_path.exists():
            return {}
        cutoff = self._clock() - timedelta(minutes=self.lookback_minutes)
        latest: dict[str, datetime] = {}
        try:
            for raw in self.journal_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "heartbeat":
                    continue
                persona = (rec.get("persona") or "").upper()
                if persona not in FLEET_PERSONAS:
                    continue
                ts_raw = rec.get("ts")
                try:
                    ts = datetime.fromisoformat(
                        str(ts_raw).replace("Z", "+00:00"),
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue
                prev = latest.get(persona)
                if prev is None or ts > prev:
                    latest[persona] = ts
        except OSError:
            return {}
        return latest

    def _classify(
        self,
        persona: str,
        last: datetime | None,
        now: datetime,
    ) -> DaemonHealth:
        if last is None:
            return DaemonHealth(
                persona=persona,
                status=HealthStatus.OFFLINE,
                last_heartbeat=None,
                minutes_since=float(self.lookback_minutes),
                note=(f"no heartbeat in last {int(self.lookback_minutes)} min"),
            )
        minutes = max(0.0, (now - last).total_seconds() / 60.0)
        if minutes >= self.offline_minutes:
            status = HealthStatus.OFFLINE
            note = f"silent for {minutes:.1f} min"
        elif minutes >= self.stuck_minutes:
            status = HealthStatus.STUCK
            note = f"stale heartbeat ({minutes:.1f} min)"
        else:
            status = HealthStatus.HEALTHY
            note = ""
        return DaemonHealth(
            persona=persona,
            status=status,
            last_heartbeat=last,
            minutes_since=minutes,
            note=note,
        )

    def sweep(self) -> WatchdogReport:
        """One-shot check of all siblings. Main entry point."""
        now = self._clock()
        latest = self._read_heartbeats()

        health: list[DaemonHealth] = []
        alerts: list[str] = []
        relaunches: list[str] = []

        for persona in FLEET_PERSONAS:
            # Don't watchdog yourself -- if we're not heartbeating we
            # wouldn't be running this sweep. Skipping also prevents
            # spurious self-alerts after a cold start.
            if self.self_persona and persona == self.self_persona:
                continue
            h = self._classify(persona, latest.get(persona), now)
            health.append(h)

            if h.status is HealthStatus.STUCK:
                alerts.append(persona)
                self._alert(persona, h, level=AlertLevel.WARN)
            elif h.status is HealthStatus.OFFLINE:
                alerts.append(persona)
                self._alert(persona, h, level=AlertLevel.CRITICAL)
                if self.relauncher is not None:
                    ok = self.relauncher.relaunch(persona)
                    if ok:
                        relaunches.append(persona)
                        self._alert_relaunch(persona)

        return WatchdogReport(
            checked_at=now,
            daemons=health,
            alerts_fired=alerts,
            relaunches=relaunches,
        )

    def _alert(
        self,
        persona: str,
        health: DaemonHealth,
        *,
        level: AlertLevel,
    ) -> None:
        try:
            self.push_bus.push(
                level=level,
                title=f"{persona} daemon {health.status.value}",
                body=(
                    f"persona={persona}\n"
                    f"status={health.status.value}\n"
                    f"last_heartbeat={health.last_heartbeat}\n"
                    f"minutes_since={health.minutes_since:.1f}\n"
                    f"note={health.note}"
                ),
                source="watchdog",
                tags=["watchdog", persona.lower(), health.status.value.lower()],
            )
        except Exception:
            # Pushing must never crash the sweep.
            return

    def _alert_relaunch(self, persona: str) -> None:
        try:
            self.push_bus.push(
                level=AlertLevel.INFO,
                title=f"{persona} daemon relaunch attempted",
                body=f"watchdog fired cold restart for {persona}",
                source="watchdog",
                tags=["watchdog", "relaunch", persona.lower()],
            )
        except Exception:
            return


__all__ = [
    "FLEET_PERSONAS",
    "DaemonHealth",
    "HealthStatus",
    "Watchdog",
    "WatchdogRelauncher",
    "WatchdogReport",
]

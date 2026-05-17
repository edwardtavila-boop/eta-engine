r"""Daemon auto-recovery watchdog (Tier-3 #9, 2026-04-27).

The VPS boot tasks restart processes that EXIT (RestartCount=999 in the
existing register_tasks.ps1). They do NOT restart processes that are
running but DEADLOCKED -- a Python daemon stuck in a blocking I/O call
or a re-entrancy lock has alive=True from the OS perspective even though
nothing's progressing.

This watchdog detects deadlocked daemons by checking heartbeat freshness.
Every daemon writes a heartbeat JSON file every N seconds; if the file
hasn't been touched in > stale_threshold_s, kill the process and let
Task Scheduler restart it.

Heartbeats expected at::

    C:\EvolutionaryTradingAlgo\var\eta_engine\state\<daemon>_heartbeat.json

Each daemon's expected heartbeat cadence is configured below.

Usage (typically via scheduled task running every 60s)::

    python -m eta_engine.obs.daemon_recovery_watchdog
    python -m eta_engine.obs.daemon_recovery_watchdog --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("daemon_recovery_watchdog")


@dataclass
class DaemonSpec:
    name: str
    heartbeat_filename: str
    stale_threshold_s: float
    process_match: str  # substring matched against full command line


# Daemons we monitor. Each must drop a heartbeat file at the expected
# cadence; tune stale_threshold_s to ~3x the cadence to absorb GC pauses
# without false-positive killing.
WATCHED_DAEMONS = [
    DaemonSpec(
        name="avengers_fleet",
        heartbeat_filename="avengers_heartbeat.json",
        stale_threshold_s=180.0,  # heartbeat every 60s; 3x = 180s
        process_match="avengers_daemon.py",
    ),
    DaemonSpec(
        name="dashboard",
        heartbeat_filename="dashboard_heartbeat.json",
        stale_threshold_s=120.0,
        process_match="deploy.scripts.dashboard_api:app",
    ),
    DaemonSpec(
        name="jarvis_live",
        heartbeat_filename="jarvis_live_heartbeat.json",
        stale_threshold_s=180.0,
        process_match="jarvis_live.py",
    ),
    DaemonSpec(
        name="jarvis_persona",
        heartbeat_filename="jarvis_daemon_heartbeat.json",
        stale_threshold_s=180.0,
        process_match="run_avenger_daemon.*--persona JARVIS",
    ),
    DaemonSpec(
        name="batman_persona",
        heartbeat_filename="batman_daemon_heartbeat.json",
        stale_threshold_s=180.0,
        process_match="run_avenger_daemon.*--persona BATMAN",
    ),
    DaemonSpec(
        name="alfred_persona",
        heartbeat_filename="alfred_daemon_heartbeat.json",
        stale_threshold_s=180.0,
        process_match="run_avenger_daemon.*--persona ALFRED",
    ),
    DaemonSpec(
        name="robin_persona",
        heartbeat_filename="robin_daemon_heartbeat.json",
        stale_threshold_s=180.0,
        process_match="run_avenger_daemon.*--persona ROBIN",
    ),
]


def heartbeat_paths_for(name: str) -> list[Path]:
    """Return the canonical heartbeat path for a watched daemon.

    The watchdog is a live safety surface, so it reads only the canonical
    workspace runtime state instead of merging legacy in-repo heartbeats.
    """
    return [workspace_roots.ETA_RUNTIME_STATE_DIR / name]


def heartbeat_age_seconds(spec: DaemonSpec) -> float | None:
    """Age in seconds of the most recent heartbeat file. None if missing."""
    for p in heartbeat_paths_for(spec.heartbeat_filename):
        if not p.exists():
            continue
        try:
            mtime = p.stat().st_mtime
            return time.time() - mtime
        except OSError:
            continue
    return None


def find_processes_matching(pattern: str) -> list[int]:
    """Return PIDs of running python processes whose cmdline matches pattern."""
    import subprocess

    try:
        # Use PowerShell to enumerate -- Win32_Process has CommandLine.
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
            "Where-Object { $_.CommandLine -match '" + pattern.replace("'", "''") + "' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        pids = [int(x) for x in result.stdout.strip().splitlines() if x.strip().isdigit()]
        return pids
    except (subprocess.SubprocessError, OSError, ValueError):
        return []


def kill_pid(pid: int, *, dry_run: bool) -> bool:
    if dry_run:
        logger.info("DRY-RUN would kill PID %d", pid)
        return True
    import subprocess

    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    revived: list[str] = []
    for spec in WATCHED_DAEMONS:
        age = heartbeat_age_seconds(spec)
        if age is None:
            logger.debug("%s: no heartbeat file -- daemon may not be running yet", spec.name)
            continue
        if age <= spec.stale_threshold_s:
            logger.debug(
                "%s: heartbeat %.1fs old (<= %.1fs threshold) -- healthy", spec.name, age, spec.stale_threshold_s
            )
            continue
        # Stale heartbeat -- find + kill the process; Task Scheduler will
        # restart it on its own restart-count policy.
        logger.warning(
            "%s: heartbeat %.1fs old (> %.1fs threshold) -- STALE; killing process(es)",
            spec.name,
            age,
            spec.stale_threshold_s,
        )
        pids = find_processes_matching(spec.process_match)
        if not pids:
            logger.warning("%s: no matching process found; nothing to kill", spec.name)
            continue
        for pid in pids:
            if kill_pid(pid, dry_run=args.dry_run):
                revived.append(f"{spec.name} (PID={pid})")

    if revived:
        logger.info("revived %d daemon(s): %s", len(revived), ", ".join(revived))
        return 1  # non-zero so Task Scheduler logs an "incident"
    logger.info("all watched daemons healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())

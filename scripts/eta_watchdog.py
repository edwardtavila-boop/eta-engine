"""Supervisor watchdog (24/7 framework, 2026-05-06).

Runs as a separate Windows scheduled task on a 60-second interval. Reads
the supervisor's heartbeat + keepalive files, decides whether the
supervisor is alive, and relaunches the wrapper script when the
heartbeat is stale or the process is gone.

Behavior matrix
---------------
| heartbeat_age      | process_alive | action                  |
|--------------------|---------------|-------------------------|
| fresh (< stale_s)  | yes           | noop                    |
| fresh              | no            | relaunch                |
| stale              | yes           | kill + relaunch         |
| stale / missing    | no            | relaunch                |

The watchdog respects an operator opt-out: if
``var/eta_engine/state/supervisor_disabled.txt`` exists, all relaunch
paths are NO-OPs (the watchdog still updates its own heartbeat so
operators can verify it ran).

Configuration env
-----------------
* ``ETA_WATCHDOG_STALE_S``        -- staleness threshold (default 300s).
* ``ETA_WATCHDOG_TASK_NAME``      -- task name passed to schtasks /Run for
  relaunch (default ``ETA-Jarvis-Strategy-Supervisor``).
* ``ETA_WATCHDOG_WRAPPER_CMD``    -- alternative: path to the wrapper .cmd.
  When set, the watchdog launches the wrapper directly with subprocess
  rather than going through Task Scheduler. Useful for unit tests.
* ``ETA_WATCHDOG_HEARTBEAT_PATH`` -- override supervisor heartbeat path.
* ``ETA_WATCHDOG_PROCESS_NAME``   -- substring to match in psutil's cmdline
  (default ``jarvis_strategy_supervisor.py``).
* ``ETA_WATCHDOG_DISABLED_FLAG``  -- override path to the opt-out file.

Always-on guarantees
--------------------
* Watchdog writes its own heartbeat at
  ``var/eta_engine/state/watchdog_heartbeat.json`` every tick — operators
  can confirm "the watchdog itself is still alive".
* Every relaunch / opt-out / noop is recorded into
  ``uptime_events.jsonl`` so post-mortems see the full sequence.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.uptime_events import record_uptime_event  # noqa: E402

logger = logging.getLogger("eta_watchdog")

# ─── Defaults ─────────────────────────────────────────────────────────────
DEFAULT_STALE_S = float(os.getenv("ETA_WATCHDOG_STALE_S", "300"))
DEFAULT_TASK_NAME = os.getenv(
    # Match the deployed scheduled-task name verbatim. The VPS task is
    # created as ``ETA-Jarvis-Strategy-Supervisor`` (hyphenated). The
    # earlier non-hyphenated default silently broke watchdog relaunches
    # with ``ERROR: The system cannot find the file specified.``.
    "ETA_WATCHDOG_TASK_NAME", "ETA-Jarvis-Strategy-Supervisor",
)
DEFAULT_PROCESS_SUBSTRING = os.getenv(
    "ETA_WATCHDOG_PROCESS_NAME", "jarvis_strategy_supervisor.py",
)
DEFAULT_HEARTBEAT_PATH = (
    workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH
)
DEFAULT_KEEPALIVE_PATH = (
    workspace_roots.ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH
)
DEFAULT_WATCHDOG_HEARTBEAT_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "watchdog_heartbeat.json"
)
DEFAULT_DISABLED_FLAG_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "supervisor_disabled.txt"
)


@dataclass(slots=True)
class WatchdogDecision:
    """Outcome of a single watchdog tick."""

    component: str
    heartbeat_age_s: float | None
    process_alive: bool
    stale: bool
    disabled_opt_out: bool
    action: str  # "noop", "relaunched", "killed_and_relaunched", "skipped_disabled"
    reason: str
    heartbeat_path: str
    process_pids_seen: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "heartbeat_age_s": self.heartbeat_age_s,
            "process_alive": self.process_alive,
            "stale": self.stale,
            "disabled_opt_out": self.disabled_opt_out,
            "action": self.action,
            "reason": self.reason,
            "heartbeat_path": self.heartbeat_path,
            "process_pids_seen": list(self.process_pids_seen),
        }


# ─── Heartbeat parsing ────────────────────────────────────────────────────
def _read_heartbeat_age_s(path: Path) -> float | None:
    """Return seconds since the heartbeat file was last refreshed.

    Reads the ``ts`` field if present (preferred), else falls back to
    the file's mtime so a watchdog can still tell freshness even if the
    heartbeat file lacks a timestamp.

    Returns
    -------
    seconds_since_last_update or None when the file is missing or
    completely unreadable.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
        ts_raw = ""
        if isinstance(payload, dict):
            ts_raw = (
                payload.get("ts")
                or payload.get("keepalive_ts")
                or payload.get("set_at_utc")
                or ""
            )
        if isinstance(ts_raw, str) and ts_raw:
            # Python 3.11+ accepts most ISO-8601 formats including "Z".
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(UTC) - ts).total_seconds())
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: file mtime in UTC.
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return max(0.0, (datetime.now(UTC) - mtime).total_seconds())
    except OSError:
        return None


# ─── Process inspection ───────────────────────────────────────────────────
def _find_pids_with_powershell(substring: str) -> list[int]:
    """Return matching process IDs using Windows CIM when psutil is absent.

    The VPS Python runtime may not have psutil installed. Rather than running
    blind, fall back to PowerShell's CIM process inventory. The search needle is
    passed through the environment so the temporary PowerShell command line does
    not accidentally match itself.
    """
    if os.name != "nt" or not substring:
        return []
    env = os.environ.copy()
    env["_ETA_WATCHDOG_PROCESS_NEEDLE"] = substring
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like ('*' + $env:_ETA_WATCHDOG_PROCESS_NEEDLE + '*') } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed executable and args
            ["powershell", "-NoProfile", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            text=True,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _find_supervisor_pids(substring: str) -> list[int]:
    """Return PIDs whose cmdline contains ``substring``.

    Uses psutil when available; falls back to PowerShell/CIM when psutil is absent.
    WARNING when psutil is missing so the watchdog stays operational
    on minimal images. The watchdog will still relaunch when the
    heartbeat is stale — it just can't preemptively kill stuck
    processes without psutil.
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        pids = _find_pids_with_powershell(substring)
        if pids:
            return pids
        logger.warning(
            "psutil unavailable and PowerShell process fallback found no matching "
            "processes for %r.",
            substring,
        )
        return pids
    pids: list[int] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if substring in cmdline:
            pids.append(int(proc.info["pid"]))
    return pids


def _kill_pids(pids: list[int], timeout_s: float = 5.0) -> list[int]:
    """Best-effort kill of supervisor PIDs. Returns the killed list."""
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        logger.warning("psutil unavailable; cannot kill stale processes.")
        return []
    killed: list[int] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        still_alive = []
        for pid in killed:
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    still_alive.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not still_alive:
            return killed
        time.sleep(0.2)
    # Hard kill anything still alive.
    for pid in still_alive:
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


# ─── Relaunch ─────────────────────────────────────────────────────────────
def _relaunch_supervisor(
    *,
    task_name: str = DEFAULT_TASK_NAME,
    wrapper_cmd: str | None = None,
) -> tuple[bool, str]:
    """Trigger a supervisor relaunch.

    Two modes, mutually exclusive:

    * Default (Windows Task Scheduler): runs ``schtasks /Run /TN <task>``.
    * Wrapper mode: when ``ETA_WATCHDOG_WRAPPER_CMD`` is set (or
      ``wrapper_cmd`` arg is non-empty), invokes the wrapper directly.
      This is the fast path tests use because Task Scheduler isn't
      available in test environments.

    Returns
    -------
    (success, reason)
    """
    cmd_override = wrapper_cmd or os.getenv("ETA_WATCHDOG_WRAPPER_CMD", "").strip()
    if cmd_override:
        try:
            proc = subprocess.Popen(  # noqa: S603 — wrapper path comes from operator env
                [cmd_override],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=False,
            )
            return True, f"wrapper_launched_pid={proc.pid}"
        except Exception as exc:  # noqa: BLE001
            return False, f"wrapper_launch_failed:{type(exc).__name__}:{exc}"

    # Default: Windows Task Scheduler.
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            timeout=30,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return True, "schtasks_run_ok"
        return False, f"schtasks_rc={result.returncode}:{result.stderr.strip()[:200]}"
    except FileNotFoundError:
        return False, "schtasks_not_found"
    except Exception as exc:  # noqa: BLE001
        return False, f"schtasks_failed:{type(exc).__name__}:{exc}"


# ─── Watchdog tick ────────────────────────────────────────────────────────
def watchdog_tick(
    *,
    component: str = "supervisor",
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    keepalive_path: Path | None = DEFAULT_KEEPALIVE_PATH,
    process_substring: str = DEFAULT_PROCESS_SUBSTRING,
    stale_s: float = DEFAULT_STALE_S,
    disabled_flag_path: Path = DEFAULT_DISABLED_FLAG_PATH,
    watchdog_heartbeat_path: Path = DEFAULT_WATCHDOG_HEARTBEAT_PATH,
    task_name: str = DEFAULT_TASK_NAME,
    wrapper_cmd: str | None = None,
    relaunch_fn: Callable[..., tuple[bool, str]] | None = None,
    pid_fn: Callable[[str], list[int]] | None = None,
    kill_fn: Callable[[list[int]], list[int]] | None = None,
) -> WatchdogDecision:
    """Run one watchdog cycle for ``component``.

    Returns a :class:`WatchdogDecision` so callers (tests, ops scripts)
    can inspect the outcome without grepping logs.
    """
    relaunch = relaunch_fn or _relaunch_supervisor
    list_pids = pid_fn or _find_supervisor_pids
    kill = kill_fn or _kill_pids

    # Heartbeat freshness: prefer keepalive when present + fresh, else
    # fall back to main heartbeat. Either being fresh proves the
    # process is doing some work.
    main_age = _read_heartbeat_age_s(heartbeat_path)
    keep_age = (
        _read_heartbeat_age_s(keepalive_path)
        if keepalive_path is not None
        else None
    )
    if main_age is None and keep_age is None:
        age = None
    elif main_age is None:
        age = keep_age
    elif keep_age is None:
        age = main_age
    else:
        age = min(main_age, keep_age)

    pids = list_pids(process_substring)
    process_alive = bool(pids)
    stale = age is None or age > stale_s
    disabled = disabled_flag_path.exists()

    decision = WatchdogDecision(
        component=component,
        heartbeat_age_s=age,
        process_alive=process_alive,
        stale=stale,
        disabled_opt_out=disabled,
        action="noop",
        reason="",
        heartbeat_path=str(heartbeat_path),
        process_pids_seen=pids,
    )

    if disabled:
        decision.action = "skipped_disabled"
        decision.reason = "supervisor_disabled.txt present"
        _record(component, decision)
        _write_watchdog_heartbeat(watchdog_heartbeat_path, decision)
        return decision

    if not stale and process_alive:
        decision.action = "noop"
        decision.reason = "fresh_heartbeat_and_process_running"
        _record(component, decision)
        _write_watchdog_heartbeat(watchdog_heartbeat_path, decision)
        return decision

    # Decide whether we kill first (heartbeat stale but process exists)
    # or just relaunch (process gone). Either way, we issue a relaunch
    # at the end.
    if process_alive and stale:
        # Stuck process: kill, then relaunch.
        killed = kill(pids)
        ok, reason = relaunch(task_name=task_name, wrapper_cmd=wrapper_cmd)
        decision.action = "killed_and_relaunched" if ok else "kill_then_relaunch_failed"
        decision.reason = (
            f"killed_pids={killed} relaunch_ok={ok} relaunch_reason={reason}"
        )
    else:
        ok, reason = relaunch(task_name=task_name, wrapper_cmd=wrapper_cmd)
        decision.action = "relaunched" if ok else "relaunch_failed"
        decision.reason = f"process_alive={process_alive} stale={stale} relaunch_reason={reason}"

    _record(component, decision)
    _write_watchdog_heartbeat(watchdog_heartbeat_path, decision)
    return decision


def _record(component: str, decision: WatchdogDecision) -> None:
    """Append the decision into uptime_events.jsonl."""
    with contextlib.suppress(Exception):
        record_uptime_event(
            component="watchdog",
            event=decision.action,
            reason=decision.reason,
            extra={
                "watched_component": component,
                "heartbeat_age_s": decision.heartbeat_age_s,
                "process_alive": decision.process_alive,
                "stale": decision.stale,
                "process_pids_seen": list(decision.process_pids_seen),
            },
        )


def _write_watchdog_heartbeat(path: Path, decision: WatchdogDecision) -> None:
    """Stamp ``var/eta_engine/state/watchdog_heartbeat.json`` so an
    operator can verify the watchdog itself ran. Atomic write — readers
    never see a partial document.
    """
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "decision": decision.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchdog heartbeat write failed: %s", exc)


# ─── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        default="supervisor",
        choices=("supervisor", "broker_router"),
        help="Which component to watchdog (default: supervisor).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit (default: 60s loop).",
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=60.0,
        help="Loop interval seconds when not --once.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.component == "broker_router":
        # Broker-router watchdog: separate heartbeat path + process
        # substring + task name, but same tick logic.
        from eta_engine.scripts import broker_router as _br_module

        # _br_module.DEFAULT_STATE_ROOT may not exist in older releases;
        # default to the canonical workspace path.
        try:
            heartbeat_path = _br_module.DEFAULT_STATE_ROOT / "broker_router_heartbeat.json"
        except AttributeError:
            heartbeat_path = (
                workspace_roots.ETA_RUNTIME_STATE_DIR
                / "router"
                / "broker_router_heartbeat.json"
            )
        kwargs = {
            "component": "broker_router",
            "heartbeat_path": heartbeat_path,
            "keepalive_path": None,
            "process_substring": os.getenv(
                "ETA_BROKER_ROUTER_WATCHDOG_PROCESS_NAME",
                "broker_router.py",
            ),
            "task_name": os.getenv(
                # Match the deployed VPS service task name. Earlier defaults
                # such as "ETA-BrokerRouter" and "ETA-BrokerRouter-Service"
                # do not exist on the live host, so relaunch attempts failed
                # exactly when the paper-live router needed recovery.
                "ETA_BROKER_ROUTER_WATCHDOG_TASK_NAME",
                "ETA-Broker-Router",
            ),
            "watchdog_heartbeat_path": (
                workspace_roots.ETA_RUNTIME_STATE_DIR
                / "broker_router_watchdog_heartbeat.json"
            ),
        }
    else:
        kwargs = {"component": "supervisor"}

    if args.once:
        decision = watchdog_tick(**kwargs)
        logger.info(
            "watchdog tick: action=%s heartbeat_age_s=%s",
            decision.action, decision.heartbeat_age_s,
        )
        return 0

    while True:
        try:
            decision = watchdog_tick(**kwargs)
            logger.info(
                "watchdog tick: action=%s heartbeat_age_s=%s",
                decision.action, decision.heartbeat_age_s,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("watchdog tick raised: %s", exc)
        time.sleep(max(1.0, float(args.interval_s)))


if __name__ == "__main__":
    sys.exit(main())

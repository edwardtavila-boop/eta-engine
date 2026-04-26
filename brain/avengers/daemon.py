"""
APEX PREDATOR  //  brain.avengers.daemon
========================================
The 24/7 background supervisor that turns each Avenger into a live
JARVIS sub-agent.

Why this exists
---------------
Operator directive (2026-04-23): "i want all of them also running live
24/7 to be jarvis sub agents."

The Fleet + Persona contract handles ONE envelope at a time -- it's the
hot-path router. To make each Avenger a persistent sub-agent we need a
tiny supervisor that:

  1. Wakes on a fixed tick (default 60 s).
  2. Walks ``dispatch.TASK_CADENCE`` and checks which ``BackgroundTask``
     values are due *right now* for my persona.
  3. Builds a ``TaskEnvelope`` for each due task and dispatches it
     through the Fleet -- so the same JARVIS pre-flight + JSONL journal
     pipeline is reused.
  4. Writes a heartbeat line to the journal every tick so the admin
     console shows "ALIVE" for every persona.
  5. Catches exceptions so a single bad envelope can't kill the daemon.

JARVIS is special: his daemon doesn't run Background tasks (he has no
LLM tier), he just emits a heartbeat + audits the *state of the other
three* (checks PID files, counts failed dispatches in the last hour,
etc.). That keeps JARVIS's role identical to what it was on the hot
path -- pure policy / admin -- while the Avengers carry the weight.

Design principles (same as the rest of brain.avengers)
------------------------------------------------------
1. Pure stdlib + pydantic. No network, no threads by default.
2. Deterministic cron matcher so tests don't need ``freezegun``.
3. Crash-resilient ``run_forever`` -- any exception is logged, the loop
   sleeps, and the daemon keeps going.
4. PID file at ``~/.jarvis/daemon_<persona>.pid`` so the operator can
   ``tasklist`` or kill the process without hunting for it.
5. Every tick is auditable -- the heartbeat is a journal line too.

Public API
----------
  * ``AvengerDaemon``      -- supervisor class
  * ``DaemonHeartbeat``    -- pydantic model appended to journal each tick
  * ``is_due``             -- pure cron matcher (exposed for tests)
  * ``envelope_for_task``  -- pure mapping BackgroundTask -> TaskEnvelope
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from apex_predator.brain.avengers.base import (
    AVENGERS_JOURNAL,
    TaskEnvelope,
    make_envelope,
)
from apex_predator.brain.avengers.dispatch import (
    TASK_CADENCE,
    TASK_OWNERS,
    BackgroundTask,
)
from apex_predator.brain.avengers.fleet import Fleet
from apex_predator.brain.jarvis_admin import SubsystemId
from apex_predator.brain.model_policy import TaskCategory

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heartbeat model
# ---------------------------------------------------------------------------


class DaemonHeartbeat(BaseModel):
    """One heartbeat per daemon tick. Appended to the avengers journal so
    the admin console can show "ALIVE 12s ago" for every persona.
    """
    model_config = ConfigDict(frozen=True)

    persona:         str = Field(min_length=1)
    pid:             int
    tick_index:      int = Field(ge=0)
    ts:              datetime
    tasks_due:       list[str] = Field(default_factory=list)
    tasks_ok:        int = Field(ge=0, default=0)
    tasks_failed:    int = Field(ge=0, default=0)
    note:            str = ""


# ---------------------------------------------------------------------------
# Cron matcher (minimal, deterministic)
# ---------------------------------------------------------------------------


def _parse_cron_field(
    expr: str, lo: int, hi: int,
) -> frozenset[int]:
    """Expand one cron field (e.g. ``"*/5"`` or ``"1,3-5"``) to a set of ints.

    Supports:
      * ``*``           -- every value in [lo, hi]
      * ``*/N``         -- every Nth value starting from ``lo``
      * ``A``           -- single integer
      * ``A-B``         -- inclusive range
      * ``A,B,C-D,*/N`` -- comma-separated combinations

    Values outside ``[lo, hi]`` are silently dropped so a bad expression
    never raises.
    """
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            values.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                continue
            if step <= 0:
                continue
            values.update(range(lo, hi + 1, step))
        elif "-" in part:
            try:
                a, b = part.split("-", 1)
                start = max(lo, int(a))
                end = min(hi, int(b))
                if start <= end:
                    values.update(range(start, end + 1))
            except ValueError:
                continue
        else:
            try:
                v = int(part)
            except ValueError:
                continue
            if lo <= v <= hi:
                values.add(v)
    return frozenset(values)


def is_due(cron_expr: str, now: datetime) -> bool:
    """Return True if ``now`` satisfies a 5-field cron expression.

    Fields (standard cron): minute, hour, day-of-month, month, day-of-week.
    ``now`` must be timezone-aware. We use local-wallclock semantics --
    the matcher reads ``now.minute``, ``now.hour`` etc. directly.
    """
    fields = cron_expr.split()
    if len(fields) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    minutes  = _parse_cron_field(minute_f, 0, 59)
    hours    = _parse_cron_field(hour_f,   0, 23)
    days     = _parse_cron_field(dom_f,    1, 31)
    months   = _parse_cron_field(month_f,  1, 12)
    # Python weekday: Monday=0 .. Sunday=6. Cron weekday: Sunday=0 .. Sat=6.
    # Accept both: build the cron-style weekday set then compare to now.
    dow_cron = _parse_cron_field(dow_f, 0, 6)
    # Translate cron-style (Sun=0) to Python (Mon=0): Python = (cron - 1) % 7
    dow_py = frozenset(((c - 1) % 7) for c in dow_cron)
    return (
        now.minute   in minutes
        and now.hour in hours
        and now.day  in days
        and now.month in months
        and now.weekday() in dow_py
    )


# ---------------------------------------------------------------------------
# BackgroundTask -> TaskEnvelope mapping
# ---------------------------------------------------------------------------


_TASK_TO_CATEGORY: dict[BackgroundTask, TaskCategory] = {
    # ALFRED lane
    BackgroundTask.KAIZEN_RETRO:       TaskCategory.DOC_WRITING,
    BackgroundTask.DISTILL_TRAIN:      TaskCategory.DATA_PIPELINE,
    BackgroundTask.SHADOW_TICK:        TaskCategory.TEST_RUN,
    BackgroundTask.DRIFT_SUMMARY:      TaskCategory.DEBUG,
    # BATMAN lane
    BackgroundTask.STRATEGY_MINE:      TaskCategory.ARCHITECTURE_DECISION,
    BackgroundTask.CAUSAL_REVIEW:      TaskCategory.ADVERSARIAL_REVIEW,
    BackgroundTask.TWIN_VERDICT:       TaskCategory.RED_TEAM_SCORING,
    BackgroundTask.DOCTRINE_REVIEW:    TaskCategory.RISK_POLICY_DESIGN,
    # ROBIN lane
    BackgroundTask.LOG_COMPACT:        TaskCategory.LOG_PARSING,
    BackgroundTask.PROMPT_WARMUP:      TaskCategory.BOILERPLATE,
    BackgroundTask.DASHBOARD_ASSEMBLE: TaskCategory.FORMATTING,
    BackgroundTask.AUDIT_SUMMARIZE:    TaskCategory.LOG_PARSING,
    # ALFRED -- meta-upgrade is routine dev work (git pull + tests + restart)
    BackgroundTask.META_UPGRADE:       TaskCategory.DATA_PIPELINE,
    # ALFRED -- monthly chaos drill = a test run of the resilience suite
    BackgroundTask.CHAOS_DRILL:        TaskCategory.TEST_RUN,
    # ALFRED -- auto-heal the fleet (check daemons alive, restart if dead)
    BackgroundTask.HEALTH_WATCHDOG:    TaskCategory.DEBUG,
    # ALFRED -- daily smoke is a cheap end-to-end test run
    BackgroundTask.SELF_TEST:          TaskCategory.TEST_RUN,
    # ROBIN -- log rotation is pure mechanical file work
    BackgroundTask.LOG_ROTATE:         TaskCategory.LOG_PARSING,
    # ROBIN -- disk cleanup is pure mechanical file work
    BackgroundTask.DISK_CLEANUP:       TaskCategory.FORMATTING,
    # ALFRED -- backup is state-pipeline work (snapshot state_dir)
    BackgroundTask.BACKUP:             TaskCategory.DATA_PIPELINE,
    # ROBIN -- metrics export is trivial formatting of a fixed template
    BackgroundTask.PROMETHEUS_EXPORT:  TaskCategory.FORMATTING,
}


_TASK_GOALS: dict[BackgroundTask, str] = {
    BackgroundTask.KAIZEN_RETRO:
        "draft the daily Kaizen retrospective from today's journal entries",
    BackgroundTask.DISTILL_TRAIN:
        "refresh the distillation classifier from the latest escalation log",
    BackgroundTask.SHADOW_TICK:
        "advance the shadow-trade ledger one tick and reconcile",
    BackgroundTask.DRIFT_SUMMARY:
        "summarize regime / feature drift over the last 15 minutes",
    BackgroundTask.STRATEGY_MINE:
        "mine the weekly rationale log for candidate strategy variants",
    BackgroundTask.CAUSAL_REVIEW:
        "run the monthly causal-ATE review on last month's promotions",
    BackgroundTask.TWIN_VERDICT:
        "produce the nightly digital-twin promote/avoid verdict",
    BackgroundTask.DOCTRINE_REVIEW:
        "run the quarterly doctrine review across active strategies",
    BackgroundTask.LOG_COMPACT:
        "compact the avengers journal: fold duplicate heartbeats into spans",
    BackgroundTask.PROMPT_WARMUP:
        "warm the persona prompt cache for pre-market and pre-close",
    BackgroundTask.DASHBOARD_ASSEMBLE:
        "rebuild the Fleet dashboard payload from the latest journal state",
    BackgroundTask.AUDIT_SUMMARIZE:
        "produce the daily audit-log summary for operator review",
    BackgroundTask.META_UPGRADE:
        "pull latest commits, run fast test suite, restart services if green",
    BackgroundTask.CHAOS_DRILL:
        "run monthly chaos drills (breaker / deadman / daemon / shared-breaker / drift)",
    BackgroundTask.HEALTH_WATCHDOG:
        "check daemon liveness and restart any that have died",
    BackgroundTask.SELF_TEST:
        "run daily end-to-end smoke: dashboard, kill switch, broker ping",
    BackgroundTask.LOG_ROTATE:
        "archive and prune the avengers JSONL + cron logs",
    BackgroundTask.DISK_CLEANUP:
        "remove stale tempdirs, sandbox debris, and old artifact files",
    BackgroundTask.BACKUP:
        "snapshot state_dir and configs to a rolling daily backup",
    BackgroundTask.PROMETHEUS_EXPORT:
        "flush the latest metrics to ~/.jarvis/metrics.prom for scraping",
}


def envelope_for_task(
    task: BackgroundTask,
    *,
    caller: SubsystemId = SubsystemId.OPERATOR,
    extra_context: dict | None = None,
) -> TaskEnvelope:
    """Translate a scheduled BackgroundTask into a TaskEnvelope.

    The envelope's category picks the tier (and therefore the persona).
    Owner mismatches are caught at Persona.dispatch via the tier-lock.
    """
    return make_envelope(
        category=_TASK_TO_CATEGORY[task],
        goal=_TASK_GOALS[task],
        caller=caller,
        rationale=f"scheduled via BackgroundTask.{task.value}",
        background_task=task.value,
        **(extra_context or {}),
    )


# ---------------------------------------------------------------------------
# Local-handler bypass + Anthropic HTTP fallback + default Fleet factory
# ---------------------------------------------------------------------------
# A "local handler" is a background task that doesn't need an LLM round-
# trip -- the daemon executes it inline and emits a journal record with
# ``provider="local_handler"``. Tests monkey-patch the helper so the bot
# can pretend a task is local even though no real handler is wired yet.
# Returning ``None`` means "no local handler matched -- fall through to
# fleet.dispatch".


def _run_local_background_task(task: BackgroundTask) -> dict | None:  # noqa: ARG001
    """Default no-op stub. Real local handlers can be registered as the
    project wires them up; until then every task falls through to the
    Fleet. Tests patch this attribute to inject a synthetic handler.
    """
    return None


def _local_handler_journal_record(
    task: BackgroundTask,
    handler_result: dict,
    *,
    ts: datetime,
    persona: str,
) -> dict:
    """Wrap a handler's return into the JSONL record the journal writes.

    PROMPT_WARMUP runs through the Anthropic API for prefix caching, so
    its `est_cost_usd` is mapped to `billable_usd` and `billing_mode`
    flips to `anthropic_api`. Other tasks are free-of-charge by default.
    """
    billable = float(handler_result.get("est_cost_usd", 0.0) or 0.0)
    is_billed = task is BackgroundTask.PROMPT_WARMUP and billable > 0.0
    return {
        "ts":      ts.isoformat(),
        "persona": persona,
        "task":    task.value,
        "result": {
            "provider":     "local_handler",
            "billing_mode": "anthropic_api" if is_billed else "free",
            "billable_usd": billable,
            **handler_result,
        },
    }


def _default_fleet() -> Fleet:
    """Construct a Fleet with default journal/executor.

    Indirection point so ``run_daemon_cli`` doesn't bake in the Fleet
    class -- tests patch this to inject a fake fleet without monkey-
    patching the symbol on the Fleet class itself.
    """
    return Fleet()


def _build_anthropic_http_client(httpx_module: object) -> object:  # noqa: ANN401
    """Build an httpx.Client for the Anthropic SDK, falling back to
    HTTP/1.1 when the runtime is missing the ``h2`` package.

    The Anthropic SDK prefers HTTP/2 because it's cheaper for prompt-
    cache reuse; on systems without h2 installed it raises at
    ``Client(http2=True)``. This helper tries h2 first, swallows the
    failure, and retries without h2 so the daemon still has an HTTP
    client to issue requests with.
    """
    limits = httpx_module.Limits(  # type: ignore[attr-defined]
        max_keepalive_connections=10,
        max_connections=20,
    )
    timeout = httpx_module.Timeout(60.0, connect=10.0)  # type: ignore[attr-defined]
    try:
        return httpx_module.Client(  # type: ignore[attr-defined]
            http2=True, limits=limits, timeout=timeout,
        )
    except Exception:  # noqa: BLE001 -- h2 missing OR any handshake-time error
        return httpx_module.Client(  # type: ignore[attr-defined]
            http2=False, limits=limits, timeout=timeout,
        )


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def _pid_path(persona: str) -> Path:
    return Path.home() / ".jarvis" / f"daemon_{persona.lower()}.pid"


def _write_pid(persona: str) -> Path:
    path = _pid_path(persona)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.warning("could not write pid file %s", path)
    return path


def _remove_pid(persona: str) -> None:
    path = _pid_path(persona)
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


class AvengerDaemon:
    """Per-persona supervisor. One daemon = one Windows process.

    The daemon is a thin loop around the Fleet:

      while alive:
          heartbeat -> journal
          for task in due_tasks(persona, now):
              fleet.dispatch(envelope_for_task(task))
          sleep(tick_seconds)

    Parameters
    ----------
    persona
        Which Avenger this daemon runs for (BATMAN / ALFRED / ROBIN /
        JARVIS). JARVIS has no BackgroundTask ownership -- it just
        heartbeats.
    fleet
        The shared Fleet. One Fleet per VPS is fine; each daemon reuses
        the same persona instances (stateless).
    tick_seconds
        How often the daemon wakes. Default 60s. Tests pass a small
        value and a ``max_ticks`` to exit cleanly.
    clock
        Injected ``() -> datetime`` callable. Tests use a deterministic
        clock; production uses ``datetime.now(UTC)``.
    sleep_fn
        Injected ``(seconds) -> None``. Tests pass a no-op; production
        uses ``time.sleep``.
    journal_path
        JSONL audit log. Defaults to ``~/.jarvis/avengers.jsonl`` so the
        daemon heartbeats interleave with normal Fleet dispatches.
    """

    def __init__(
        self,
        *,
        persona: str,
        fleet: Fleet,
        tick_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        journal_path: Path | None = None,
    ) -> None:
        self.persona = persona.upper()
        self.fleet = fleet
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep_fn or time.sleep
        self._journal_path = journal_path or AVENGERS_JOURNAL
        self._alive = True
        self._tick_index = 0
        self._last_fire: dict[BackgroundTask, datetime] = {}

    # --- control -----------------------------------------------------------

    def stop(self) -> None:
        """Flag the daemon to exit at the next loop boundary."""
        self._alive = False

    def _install_signal_handlers(self) -> None:
        """Best-effort SIGINT / SIGTERM so ``Ctrl-C`` exits cleanly."""
        def _handler(
            _signo: int,
            _frame: object | None,
        ) -> None:
            self._alive = False

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            # signal.signal only works from main thread on Windows; the
            # daemon must survive if it's invoked from a worker thread.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _handler)

    # --- schedule ----------------------------------------------------------

    def due_tasks(self, now: datetime) -> list[BackgroundTask]:
        """Return every BackgroundTask that:
          * is owned by my persona, and
          * the cron expression fires for this minute, and
          * hasn't already fired within this same minute (dedupe).
        """
        due: list[BackgroundTask] = []
        this_minute = now.replace(second=0, microsecond=0)
        for task, cron_expr in TASK_CADENCE.items():
            if TASK_OWNERS.get(task, "").upper() != self.persona:
                continue
            if not is_due(cron_expr, now):
                continue
            # Dedupe within the same wall minute
            last = self._last_fire.get(task)
            if last is not None and last >= this_minute:
                continue
            self._last_fire[task] = this_minute
            due.append(task)
        return due

    # --- tick + run --------------------------------------------------------

    def tick(self) -> DaemonHeartbeat:
        """Run exactly one tick. Returns the heartbeat (for tests)."""
        now = self._clock()
        tasks_ok = 0
        tasks_failed = 0
        tasks_due_names: list[str] = []

        # JARVIS has no BackgroundTask lane -- heartbeat only.
        if self.persona != "JARVIS":
            for task in self.due_tasks(now):
                tasks_due_names.append(task.value)
                try:
                    # Local-handler bypass: if the module-level helper
                    # returns a dict, the task is handled inline and
                    # the Fleet is not invoked. Saves an LLM round-trip
                    # for tasks that don't need one (dashboard payload
                    # assembly, log compaction, prompt-cache warmup).
                    local_result = _run_local_background_task(task)
                    if local_result is not None:
                        record = _local_handler_journal_record(
                            task, local_result,
                            ts=now, persona=self.persona,
                        )
                        self._append_record(record)
                        tasks_ok += 1
                        continue
                    env = envelope_for_task(task, caller=SubsystemId.OPERATOR)
                    res = self.fleet.dispatch(env)
                    if res.success:
                        tasks_ok += 1
                    else:
                        tasks_failed += 1
                except Exception as exc:  # noqa: BLE001 -- daemon never
                    # lets one bad envelope kill the loop.
                    logger.exception(
                        "persona=%s task=%s dispatch raised: %s",
                        self.persona, task.value, exc,
                    )
                    tasks_failed += 1

        hb = DaemonHeartbeat(
            persona=self.persona,
            pid=os.getpid(),
            tick_index=self._tick_index,
            ts=now,
            tasks_due=tasks_due_names,
            tasks_ok=tasks_ok,
            tasks_failed=tasks_failed,
            note=self._admin_note(now) if self.persona == "JARVIS" else "",
        )
        self._append_heartbeat(hb)
        self._tick_index += 1
        return hb

    def run_forever(self, *, max_ticks: int | None = None) -> int:
        """Loop forever. Returns the number of ticks actually run.

        ``max_ticks`` bounds the loop (tests use it). Production leaves
        it ``None`` and the daemon runs until SIGINT.
        """
        self._install_signal_handlers()
        _write_pid(self.persona)
        logger.info(
            "AvengerDaemon[%s] starting: tick=%.1fs pid=%d journal=%s",
            self.persona, self.tick_seconds, os.getpid(), self._journal_path,
        )
        ticks_run = 0
        try:
            while self._alive:
                if max_ticks is not None and ticks_run >= max_ticks:
                    break
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 -- crash-resilient
                    logger.exception(
                        "AvengerDaemon[%s] tick raised: %s",
                        self.persona, exc,
                    )
                ticks_run += 1
                if self._alive and (max_ticks is None or ticks_run < max_ticks):
                    self._sleep(self.tick_seconds)
        finally:
            _remove_pid(self.persona)
            logger.info(
                "AvengerDaemon[%s] stopped after %d ticks",
                self.persona, ticks_run,
            )
        return ticks_run

    # --- internal ----------------------------------------------------------

    def _admin_note(self, now: datetime) -> str:
        """JARVIS-only admin note: reports which sibling PID files exist."""
        parts: list[str] = []
        for other in ("BATMAN", "ALFRED", "ROBIN"):
            pid_path = _pid_path(other)
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
                except (OSError, ValueError):
                    pid = 0
                parts.append(f"{other}=pid:{pid}")
            else:
                parts.append(f"{other}=OFFLINE")
        _ = now  # reserved for future use (last_fire-age checks, etc.)
        return "; ".join(parts)

    def _append_heartbeat(self, hb: DaemonHeartbeat) -> None:
        """Write one heartbeat line to the journal. Best-effort."""
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._journal_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts":       hb.ts.isoformat(),
                            "kind":     "heartbeat",
                            "persona":  f"persona.{hb.persona.lower()}",
                            "envelope": None,
                            "result":   None,
                            "heartbeat": hb.model_dump(mode="json"),
                        },
                        default=str,
                    )
                    + "\n",
                )
        except OSError:
            return

    def _append_record(self, record: dict) -> None:
        """Write an arbitrary JSONL record to the journal. Best-effort.

        Used by the local-handler bypass path so tests + audit code
        can find a `provider="local_handler"` entry alongside the
        usual heartbeat / dispatch lines.
        """
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            return


# ---------------------------------------------------------------------------
# Convenience: run the daemon for a persona name (used by scripts/)
# ---------------------------------------------------------------------------


VALID_PERSONAS: frozenset[str] = frozenset({"JARVIS", "BATMAN", "ALFRED", "ROBIN"})


def run_daemon_cli(
    persona: str,
    *,
    fleet: Fleet | None = None,
    tick_seconds: float = 60.0,
    max_ticks: int | None = None,
) -> int:
    """Entry point the ``scripts/run_avenger_daemon.py`` script calls.

    Keeping the plumbing here means the script itself stays a one-liner
    and the logic is covered by the same unit tests.
    """
    name = persona.upper()
    if name not in VALID_PERSONAS:
        valid = ", ".join(sorted(VALID_PERSONAS))
        raise ValueError(
            f"unknown persona {persona!r}; expected one of: {valid}",
        )
    fleet = fleet or _default_fleet()
    daemon = AvengerDaemon(
        persona=name,
        fleet=fleet,
        tick_seconds=tick_seconds,
    )
    return daemon.run_forever(max_ticks=max_ticks)


__all__ = [
    "VALID_PERSONAS",
    "AvengerDaemon",
    "DaemonHeartbeat",
    "envelope_for_task",
    "is_due",
    "run_daemon_cli",
]

"""Agent coordination protocol — file-based task queue for multi-AI sessions.

Three AI assistants (Claude, Codex, DeepSeek) share this workspace and need
to claim work atomically without colliding. This module is the coordination
primitive every session imports.

Design notes
------------
- Single canonical write path: everything under
  ``C:/EvolutionaryTradingAlgo/var/eta_engine/state/agent_coordination/``.
  No new code, state, or logs land outside the workspace root. Per CLAUDE.md
  hard rule #1.
- Atomic claim: ``os.replace`` on Windows + a short-lived sentinel file in
  ``locks/<task_id>.lock``. If two agents race to claim the same task, only
  one wins (the other gets ``ClaimError``).
- Append-only journal: ``coordination_journal.jsonl`` records every claim,
  complete, block, and reclaim. Never rewritten — debug trail for the operator.
- No trading-state side effects: this module strictly manipulates files in
  the coordination dir. It does not touch broker connections, order routers,
  or live state. Safe to run from any agent context.
- Heartbeat staleness: if an agent's ``agents/<id>.heartbeat.json`` is older
  than ``ETA_AGENT_HEARTBEAT_STALE_MIN`` minutes (default 30), its claimed
  tasks auto-return to pending so other agents can pick them up.

Per CLAUDE.md: agents claim tasks and do work, but COMMITS still go through
human approval. This module deliberately does not run git.

CLI usage
---------
    python -m eta_engine.scripts.agent_coordinator heartbeat --agent claude
    python -m eta_engine.scripts.agent_coordinator list-pending
    python -m eta_engine.scripts.agent_coordinator claim --agent claude --task T-...
    python -m eta_engine.scripts.agent_coordinator complete \
        --agent claude --task T-... --deliverable file.py --summary 'short'
    python -m eta_engine.scripts.agent_coordinator block \
        --agent claude --task T-... --reason 'needs operator decision'
    python -m eta_engine.scripts.agent_coordinator reclaim-stale
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

WORKSPACE_ROOT = Path("C:/EvolutionaryTradingAlgo")
DEFAULT_STATE_ROOT = (
    WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "agent_coordination"
)
KNOWN_AGENTS: tuple[str, ...] = ("claude", "codex", "deepseek")
DEFAULT_HEARTBEAT_STALE_MIN = 30


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CoordinatorError(Exception):
    """Base error for the coordinator module."""


class ClaimError(CoordinatorError):
    """Raised when a claim races and the caller loses."""


class TaskNotFoundError(CoordinatorError):
    """Raised when an operation references a task that doesn't exist."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with second precision (Z suffix)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat_stale_threshold() -> timedelta:
    """Read the staleness threshold from env, default 30 min.

    Operator may want to widen during long-running ops sessions.
    """
    raw = os.environ.get("ETA_AGENT_HEARTBEAT_STALE_MIN")
    if raw:
        try:
            return timedelta(minutes=int(raw))
        except ValueError:
            pass
    return timedelta(minutes=DEFAULT_HEARTBEAT_STALE_MIN)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: write to .tmp then os.replace.

    os.replace is the cross-platform atomic-rename primitive. On Windows
    it's the only safe way to overwrite an existing file without a brief
    window where readers see no file at all.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise CoordinatorError(f"task file {path} is not a YAML mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, text)


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------


@dataclass
class _Paths:
    """Pre-resolved paths under the state root. Cached per-coordinator."""

    state_root: Path
    pending: Path
    in_progress: Path
    completed: Path
    blocked: Path
    agents: Path
    locks: Path
    journal: Path

    @classmethod
    def from_root(cls, state_root: Path) -> _Paths:
        return cls(
            state_root=state_root,
            pending=state_root / "tasks" / "pending",
            in_progress=state_root / "tasks" / "in_progress",
            completed=state_root / "tasks" / "completed",
            blocked=state_root / "tasks" / "blocked",
            agents=state_root / "agents",
            locks=state_root / "locks",
            journal=state_root / "coordination_journal.jsonl",
        )


class AgentCoordinator:
    """File-based task queue + heartbeat tracker for multi-AI sessions.

    Instantiate once per agent session. The agent_id should match one of
    KNOWN_AGENTS but isn't enforced — operator can spin up a new agent
    name and it'll get its own in_progress/<name>/ subdir on first claim.
    """

    def __init__(
        self, agent_id: str, state_root: Path | None = None
    ) -> None:
        if not agent_id or "/" in agent_id or "\\" in agent_id:
            raise ValueError(f"invalid agent_id: {agent_id!r}")
        self.agent_id = agent_id
        self.state_root = state_root or DEFAULT_STATE_ROOT
        self.paths = _Paths.from_root(self.state_root)
        # Make sure the agent's in_progress dir exists. Cheap, idempotent.
        (self.paths.in_progress / agent_id).mkdir(parents=True, exist_ok=True)
        for d in (
            self.paths.pending,
            self.paths.completed,
            self.paths.blocked,
            self.paths.agents,
            self.paths.locks,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ----- heartbeat ------------------------------------------------------

    def emit_heartbeat(self) -> None:
        """Write/refresh ``agents/<agent_id>.heartbeat.json``.

        Includes ts + pid + currently-claimed task ids so operator can see
        at a glance what each agent is holding.
        """
        claimed = self._currently_claimed_ids()
        payload = {
            "agent_id": self.agent_id,
            "pid": os.getpid(),
            "ts": _utc_now_iso(),
            "claimed_tasks": claimed,
        }
        path = self.paths.agents / f"{self.agent_id}.heartbeat.json"
        _atomic_write_text(path, json.dumps(payload, indent=2) + "\n")

    def _currently_claimed_ids(self) -> list[str]:
        d = self.paths.in_progress / self.agent_id
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.yaml"))

    # ----- discovery ------------------------------------------------------

    def list_pending(
        self, *, preferred_agent: str | None = None
    ) -> list[dict[str, Any]]:
        """Return pending tasks the caller is eligible to claim.

        ``preferred_agent`` filter: if a task has ``preferred_agent: codex``
        and the caller is claude, the task is skipped UNLESS ``preferred_agent``
        is "any" or matches the caller. Tasks marked any are always shown.
        """
        out: list[dict[str, Any]] = []
        for path in sorted(self.paths.pending.glob("*.yaml")):
            try:
                task = _read_yaml(path)
            except (yaml.YAMLError, CoordinatorError):
                # Skip malformed task files; don't crash discovery for one
                # bad file. Operator will see it in a normal `ls`.
                continue
            pref = task.get("preferred_agent", "any")
            if preferred_agent and pref not in ("any", preferred_agent):
                continue
            out.append(task)
        return out

    # ----- claim / complete / block --------------------------------------

    def claim(self, task_id: str) -> dict[str, Any]:
        """Atomic-move ``pending/<id>.yaml`` -> ``in_progress/<agent>/<id>.yaml``.

        Race protection: we create a sentinel lock file with O_EXCL first.
        Only one writer can create the same lock. Whoever wins moves the
        task file; whoever loses sees the lock and raises ClaimError.

        After a successful os.replace the lock is removed. If we crash
        between the lock creation and the replace, the lock acts as a
        soft-block until ``reclaim_stale`` runs (which removes orphaned
        locks alongside reclaiming tasks from dead agents).
        """
        src = self.paths.pending / f"{task_id}.yaml"
        if not src.exists():
            raise TaskNotFoundError(f"no pending task {task_id}")

        lock_path = self.paths.locks / f"{task_id}.lock"
        try:
            # O_CREAT | O_EXCL is the atomic-create primitive. If the file
            # exists, this fails with FileExistsError — that's a lost race.
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as e:
            raise ClaimError(
                f"task {task_id} is already locked by another claimer"
            ) from e
        try:
            os.write(
                fd,
                f"{self.agent_id}\n{os.getpid()}\n{_utc_now_iso()}\n".encode(),
            )
        finally:
            os.close(fd)

        try:
            # Re-check src under the lock — another agent could have
            # completed/blocked the task between our exists() and here.
            if not src.exists():
                raise TaskNotFoundError(
                    f"task {task_id} disappeared during claim"
                )
            task = _read_yaml(src)
            task["status"] = TaskStatus.IN_PROGRESS.value
            task["agent"] = self.agent_id
            task["claimed_at"] = _utc_now_iso()
            dst = self.paths.in_progress / self.agent_id / f"{task_id}.yaml"
            _write_yaml(dst, task)
            os.replace(dst, dst)  # no-op confirm; dst already written above
            src.unlink()  # finally remove the pending entry
        finally:
            # Always release the lock so reclaim_stale doesn't have to.
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()

        self.journal(
            "claim",
            task_id,
            detail=f"by {self.agent_id} title={task.get('title', '')!r}",
        )
        return task

    def append_note(self, task_id: str, note: str) -> None:
        """Append a single note line to the task's notes[]."""
        path = self._locate_task(task_id)
        task = _read_yaml(path)
        notes = task.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        notes.append({"ts": _utc_now_iso(), "agent": self.agent_id, "note": note})
        task["notes"] = notes
        _write_yaml(path, task)

    def complete(
        self,
        task_id: str,
        *,
        deliverable_refs: list[str],
        summary: str,
    ) -> None:
        """Move ``in_progress/<agent>/<id>.yaml`` -> ``completed/<id>.yaml``."""
        src = self.paths.in_progress / self.agent_id / f"{task_id}.yaml"
        if not src.exists():
            raise TaskNotFoundError(
                f"agent {self.agent_id} has no in-progress {task_id}"
            )
        task = _read_yaml(src)
        task["status"] = TaskStatus.COMPLETED.value
        task["completed_at"] = _utc_now_iso()
        task["deliverable_refs"] = list(deliverable_refs)
        notes = task.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        notes.append(
            {
                "ts": _utc_now_iso(),
                "agent": self.agent_id,
                "note": f"completed: {summary}",
            }
        )
        task["notes"] = notes
        dst = self.paths.completed / f"{task_id}.yaml"
        _write_yaml(dst, task)
        src.unlink()
        self.journal(
            "complete", task_id, detail=f"by {self.agent_id} summary={summary!r}"
        )

    def block(self, task_id: str, *, reason: str) -> None:
        """Move task to ``blocked/`` with reason. Operator must clear."""
        src = self.paths.in_progress / self.agent_id / f"{task_id}.yaml"
        if not src.exists():
            raise TaskNotFoundError(
                f"agent {self.agent_id} has no in-progress {task_id}"
            )
        task = _read_yaml(src)
        task["status"] = TaskStatus.BLOCKED.value
        task["blocked_at"] = _utc_now_iso()
        task["block_reason"] = reason
        notes = task.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        notes.append(
            {
                "ts": _utc_now_iso(),
                "agent": self.agent_id,
                "note": f"BLOCKED: {reason}",
            }
        )
        task["notes"] = notes
        dst = self.paths.blocked / f"{task_id}.yaml"
        _write_yaml(dst, task)
        src.unlink()
        self.journal(
            "block", task_id, detail=f"by {self.agent_id} reason={reason!r}"
        )

    # ----- journal --------------------------------------------------------

    def journal(self, action: str, task_id: str, *, detail: str = "") -> None:
        """Append an entry to ``coordination_journal.jsonl``.

        Append-only. If the journal file disappears we recreate it; we
        never rewrite past entries.
        """
        entry = {
            "ts": _utc_now_iso(),
            "agent": self.agent_id,
            "action": action,
            "task_id": task_id,
            "detail": detail,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # Plain text-append. JSONL is line-oriented so a partial write would
        # at worst leave a malformed last line; readers can skip it.
        with self.paths.journal.open("a", encoding="utf-8") as fh:
            fh.write(line)

    # ----- reclaim --------------------------------------------------------

    def reclaim_stale(
        self, *, max_age_min: int | None = None
    ) -> list[str]:
        """Return tasks held by agents whose heartbeats are too old.

        For each agent under ``in_progress/<agent>/``, check
        ``agents/<agent>.heartbeat.json``. If the heartbeat is missing or
        older than the threshold, move that agent's tasks back to
        ``pending/`` so any agent can re-claim them.

        Also sweeps orphaned locks older than the threshold — locks should
        be released within milliseconds of a claim, so an old one means
        the claimer crashed.

        Returns the list of reclaimed task ids.
        """
        threshold = (
            timedelta(minutes=max_age_min)
            if max_age_min is not None
            else _heartbeat_stale_threshold()
        )
        now = datetime.now(UTC)
        reclaimed: list[str] = []

        for agent_dir in self.paths.in_progress.iterdir():
            if not agent_dir.is_dir():
                continue
            agent = agent_dir.name
            hb_path = self.paths.agents / f"{agent}.heartbeat.json"
            stale = True
            if hb_path.exists():
                try:
                    hb = json.loads(hb_path.read_text(encoding="utf-8"))
                    ts = datetime.strptime(
                        hb["ts"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=UTC)
                    stale = (now - ts) > threshold
                except (json.JSONDecodeError, KeyError, ValueError):
                    stale = True
            if not stale:
                continue
            for task_path in agent_dir.glob("*.yaml"):
                task = _read_yaml(task_path)
                task["status"] = TaskStatus.PENDING.value
                task["agent"] = None
                task["claimed_at"] = None
                notes = task.get("notes") or []
                if not isinstance(notes, list):
                    notes = []
                notes.append(
                    {
                        "ts": _utc_now_iso(),
                        "agent": "coordinator",
                        "note": (
                            f"reclaimed from stale agent {agent} "
                            f"(heartbeat older than {threshold})"
                        ),
                    }
                )
                task["notes"] = notes
                dst = self.paths.pending / task_path.name
                _write_yaml(dst, task)
                task_path.unlink()
                reclaimed.append(task_path.stem)
                self.journal(
                    "reclaim",
                    task_path.stem,
                    detail=f"from stale agent {agent}",
                )

        # Also nuke orphaned locks older than threshold. A live agent
        # holds a lock only between os.open() and os.replace() — milliseconds.
        for lock in self.paths.locks.glob("*.lock"):
            try:
                age = now - datetime.fromtimestamp(
                    lock.stat().st_mtime, tz=UTC
                )
            except OSError:
                continue
            if age > threshold:
                with contextlib.suppress(FileNotFoundError):
                    lock.unlink()

        return reclaimed

    # ----- internals ------------------------------------------------------

    def _locate_task(self, task_id: str) -> Path:
        """Find a task file across pending/in_progress/blocked.

        Used by append_note which can be called against any non-completed
        task. Completed tasks are immutable.
        """
        candidates = [
            self.paths.pending / f"{task_id}.yaml",
            self.paths.in_progress / self.agent_id / f"{task_id}.yaml",
            self.paths.blocked / f"{task_id}.yaml",
        ]
        # Also check other agents' in_progress dirs in case operator wants
        # to attach a cross-agent note — we don't enforce same-agent locking
        # on notes since notes are append-only and idempotent.
        for agent_dir in self.paths.in_progress.iterdir():
            if agent_dir.is_dir():
                candidates.append(agent_dir / f"{task_id}.yaml")
        for c in candidates:
            if c.exists():
                return c
        raise TaskNotFoundError(f"task {task_id} not found in any queue")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_pending(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("(no pending tasks)")
        return
    for t in tasks:
        tid = t.get("id", "?")
        title = t.get("title", "")
        prio = t.get("priority", "")
        pref = t.get("preferred_agent", "any")
        print(f"  {tid}  [{prio}] pref={pref}  {title}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="agent_coordinator",
        description="Multi-agent task queue (Claude / Codex / DeepSeek).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("heartbeat", help="emit a heartbeat for the agent")
    sp.add_argument("--agent", required=True)

    sp = sub.add_parser("list-pending", help="list pending tasks")
    sp.add_argument(
        "--preferred",
        default=None,
        help="filter by preferred_agent (claude/codex/deepseek)",
    )

    sp = sub.add_parser("claim", help="claim a pending task")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--task", required=True)

    sp = sub.add_parser("note", help="append a note to a task")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument("--text", required=True)

    sp = sub.add_parser("complete", help="mark a task complete")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument(
        "--deliverable",
        action="append",
        default=[],
        help="repeatable; each value is a deliverable file path",
    )
    sp.add_argument("--summary", required=True)

    sp = sub.add_parser("block", help="mark a task blocked")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument("--reason", required=True)

    sp = sub.add_parser("reclaim-stale", help="release stale agents' tasks")
    sp.add_argument("--agent", default="coordinator")
    sp.add_argument(
        "--max-age-min",
        type=int,
        default=None,
        help="override staleness threshold (default 30 min, env-configurable)",
    )

    args = p.parse_args(argv)

    if args.cmd == "heartbeat":
        c = AgentCoordinator(args.agent)
        c.emit_heartbeat()
        print(f"heartbeat ok for {args.agent}")
        return 0
    if args.cmd == "list-pending":
        c = AgentCoordinator(args.preferred or "anonymous")
        tasks = c.list_pending(preferred_agent=args.preferred)
        _print_pending(tasks)
        return 0
    if args.cmd == "claim":
        c = AgentCoordinator(args.agent)
        try:
            task = c.claim(args.task)
        except (ClaimError, TaskNotFoundError) as e:
            print(f"ERR: {e}", file=sys.stderr)
            return 2
        print(f"claimed {task['id']} -> {args.agent}")
        return 0
    if args.cmd == "note":
        c = AgentCoordinator(args.agent)
        c.append_note(args.task, args.text)
        print("ok")
        return 0
    if args.cmd == "complete":
        c = AgentCoordinator(args.agent)
        c.complete(
            args.task,
            deliverable_refs=args.deliverable,
            summary=args.summary,
        )
        print(f"completed {args.task}")
        return 0
    if args.cmd == "block":
        c = AgentCoordinator(args.agent)
        c.block(args.task, reason=args.reason)
        print(f"blocked {args.task}")
        return 0
    if args.cmd == "reclaim-stale":
        c = AgentCoordinator(args.agent)
        ids = c.reclaim_stale(max_age_min=args.max_age_min)
        if ids:
            print("reclaimed:")
            for tid in ids:
                print(f"  {tid}")
        else:
            print("(nothing to reclaim)")
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via CLI
    raise SystemExit(main())

"""Tests for the agent coordination protocol.

Covers:
- Atomic-claim race (two threads, one wins, one ClaimError)
- Heartbeat round-trip
- Reclaim-stale: simulate stale agent, confirm tasks go back to pending
- Journal append-only: confirm prior entries don't get rewritten
- Complete moves to right dir with right metadata
- Block moves to blocked/ and notes the reason
- CLI parses correctly
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

# Make sure the workspace root is on sys.path so `eta_engine` resolves.
_HERE = Path(__file__).resolve()
_ETA_ENGINE = _HERE.parents[1]
_WORKSPACE = _ETA_ENGINE.parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from eta_engine.scripts import agent_coordinator as ac  # noqa: E402,I001


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def state_root(tmp_path: Path) -> Path:
    """Fresh state tree per test, isolated from the real workspace."""
    root = tmp_path / "agent_coordination"
    # Pre-create the bare structure mirror so coordinator just attaches.
    for sub in (
        "tasks/pending",
        "tasks/in_progress/claude",
        "tasks/in_progress/codex",
        "tasks/in_progress/deepseek",
        "tasks/completed",
        "tasks/blocked",
        "agents",
        "locks",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _write_pending_task(
    root: Path, task_id: str, *, preferred_agent: str = "any"
) -> Path:
    payload = {
        "id": task_id,
        "title": f"test task {task_id}",
        "created": "2026-05-05T00:00:00Z",
        "created_by": "test",
        "priority": "P1",
        "preferred_agent": preferred_agent,
        "deliverables": ["something.py"],
        "constraints": [],
        "status": "pending",
        "agent": None,
        "claimed_at": None,
        "completed_at": None,
        "notes": [],
        "deliverable_refs": [],
    }
    path = root / "tasks" / "pending" / f"{task_id}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------


def test_heartbeat_round_trip(state_root: Path) -> None:
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.emit_heartbeat()
    hb_path = state_root / "agents" / "claude.heartbeat.json"
    assert hb_path.exists()
    payload = json.loads(hb_path.read_text(encoding="utf-8"))
    assert payload["agent_id"] == "claude"
    assert payload["pid"] == os.getpid()
    assert payload["claimed_tasks"] == []
    # Re-emit and confirm ts updates.
    first_ts = payload["ts"]
    time.sleep(1.1)
    c.emit_heartbeat()
    payload2 = json.loads(hb_path.read_text(encoding="utf-8"))
    assert payload2["ts"] != first_ts


def test_heartbeat_includes_claimed_tasks(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-100")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.claim("T-2026-05-05-100")
    c.emit_heartbeat()
    hb = json.loads(
        (state_root / "agents" / "claude.heartbeat.json").read_text(
            encoding="utf-8"
        )
    )
    assert hb["claimed_tasks"] == ["T-2026-05-05-100"]


# --------------------------------------------------------------------------
# Claim race
# --------------------------------------------------------------------------


def test_claim_basic_path(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-200")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    task = c.claim("T-2026-05-05-200")
    assert task["agent"] == "claude"
    assert task["status"] == "in_progress"
    # File should be in claude's in_progress, gone from pending.
    assert not (state_root / "tasks/pending/T-2026-05-05-200.yaml").exists()
    assert (
        state_root / "tasks/in_progress/claude/T-2026-05-05-200.yaml"
    ).exists()


def test_claim_missing_task_raises(state_root: Path) -> None:
    c = ac.AgentCoordinator("claude", state_root=state_root)
    with pytest.raises(ac.TaskNotFoundError):
        c.claim("T-DOES-NOT-EXIST")


def test_claim_race_two_threads(state_root: Path) -> None:
    """Two threads try to claim the same task. Exactly one must win."""
    _write_pending_task(state_root, "T-2026-05-05-300")
    results: dict[str, Any] = {}
    barrier = threading.Barrier(2)

    def worker(agent: str) -> None:
        c = ac.AgentCoordinator(agent, state_root=state_root)
        barrier.wait()
        try:
            c.claim("T-2026-05-05-300")
            results[agent] = "won"
        except ac.ClaimError:
            results[agent] = "lost"
        except ac.TaskNotFoundError:
            # Acceptable race: the other thread's os.replace already removed
            # the source. Treat as "lost the race".
            results[agent] = "lost"

    t1 = threading.Thread(target=worker, args=("claude",))
    t2 = threading.Thread(target=worker, args=("codex",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    won = [k for k, v in results.items() if v == "won"]
    lost = [k for k, v in results.items() if v == "lost"]
    assert len(won) == 1, f"expected 1 winner, got {results}"
    assert len(lost) == 1, f"expected 1 loser, got {results}"
    # Winner's in_progress holds the task; loser's does not.
    winner = won[0]
    assert (
        state_root
        / f"tasks/in_progress/{winner}/T-2026-05-05-300.yaml"
    ).exists()


def test_claim_releases_lock_on_success(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-310")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.claim("T-2026-05-05-310")
    assert not (state_root / "locks/T-2026-05-05-310.lock").exists()


def test_claim_refuses_task_preferred_for_another_agent(state_root: Path) -> None:
    _write_pending_task(
        state_root,
        "T-2026-05-05-320",
        preferred_agent="claude",
    )
    c = ac.AgentCoordinator("codex", state_root=state_root)

    with pytest.raises(ac.PreferredAgentError, match="preferred_agent=claude"):
        c.claim("T-2026-05-05-320")

    assert (state_root / "tasks/pending/T-2026-05-05-320.yaml").exists()
    assert not (
        state_root / "tasks/in_progress/codex/T-2026-05-05-320.yaml"
    ).exists()
    assert not (state_root / "locks/T-2026-05-05-320.lock").exists()


def test_claim_can_force_cross_agent_preference(state_root: Path) -> None:
    _write_pending_task(
        state_root,
        "T-2026-05-05-330",
        preferred_agent="claude",
    )
    c = ac.AgentCoordinator("codex", state_root=state_root)

    task = c.claim("T-2026-05-05-330", force_preferred=True)

    assert task["agent"] == "codex"
    assert (
        state_root / "tasks/in_progress/codex/T-2026-05-05-330.yaml"
    ).exists()


# --------------------------------------------------------------------------
# Append note
# --------------------------------------------------------------------------


def test_append_note(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-400")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.claim("T-2026-05-05-400")
    c.append_note("T-2026-05-05-400", "halfway through")
    path = state_root / "tasks/in_progress/claude/T-2026-05-05-400.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert len(payload["notes"]) == 1
    assert payload["notes"][0]["note"] == "halfway through"
    assert payload["notes"][0]["agent"] == "claude"


# --------------------------------------------------------------------------
# Complete
# --------------------------------------------------------------------------


def test_complete_moves_task(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-500")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.claim("T-2026-05-05-500")
    c.complete(
        "T-2026-05-05-500",
        deliverable_refs=["foo/bar.py"],
        summary="all green",
    )
    src = state_root / "tasks/in_progress/claude/T-2026-05-05-500.yaml"
    dst = state_root / "tasks/completed/T-2026-05-05-500.yaml"
    assert not src.exists()
    assert dst.exists()
    payload = yaml.safe_load(dst.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["deliverable_refs"] == ["foo/bar.py"]
    assert payload["completed_at"] is not None
    # Last note records completion summary.
    assert any(
        n["note"].startswith("completed: all green") for n in payload["notes"]
    )


def test_complete_without_claim_raises(state_root: Path) -> None:
    c = ac.AgentCoordinator("claude", state_root=state_root)
    with pytest.raises(ac.TaskNotFoundError):
        c.complete("T-NOPE", deliverable_refs=[], summary="x")


# --------------------------------------------------------------------------
# Block
# --------------------------------------------------------------------------


def test_block_moves_to_blocked(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-600")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.claim("T-2026-05-05-600")
    c.block("T-2026-05-05-600", reason="needs operator approval for live route")
    src = state_root / "tasks/in_progress/claude/T-2026-05-05-600.yaml"
    dst = state_root / "tasks/blocked/T-2026-05-05-600.yaml"
    assert not src.exists()
    assert dst.exists()
    payload = yaml.safe_load(dst.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert "operator approval" in payload["block_reason"]
    assert any("BLOCKED" in n["note"] for n in payload["notes"])


# --------------------------------------------------------------------------
# Journal append-only
# --------------------------------------------------------------------------


def test_journal_append_only(state_root: Path) -> None:
    c = ac.AgentCoordinator("claude", state_root=state_root)
    c.journal("first", "T-X", detail="initial")
    c.journal("second", "T-Y", detail="next")
    c.journal("third", "T-Z", detail="last")
    journal = state_root / "coordination_journal.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    actions = [json.loads(line)["action"] for line in lines]
    assert actions == ["first", "second", "third"]
    # Now do a real claim and confirm earlier journal lines are untouched.
    _write_pending_task(state_root, "T-2026-05-05-700")
    c.claim("T-2026-05-05-700")
    lines2 = journal.read_text(encoding="utf-8").splitlines()
    # Line count grew by exactly 1 (the claim entry).
    assert len(lines2) == 4
    # First three lines are byte-identical to before.
    assert lines2[:3] == lines


# --------------------------------------------------------------------------
# Reclaim stale
# --------------------------------------------------------------------------


def _force_heartbeat_age(
    state_root: Path, agent: str, *, age: timedelta
) -> None:
    hb = state_root / "agents" / f"{agent}.heartbeat.json"
    payload = json.loads(hb.read_text(encoding="utf-8"))
    old_ts = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["ts"] = old_ts
    hb.write_text(json.dumps(payload), encoding="utf-8")


def test_reclaim_stale_returns_tasks_to_pending(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-800")
    claude = ac.AgentCoordinator("claude", state_root=state_root)
    claude.claim("T-2026-05-05-800")
    claude.emit_heartbeat()
    # Force the heartbeat to be 60 minutes old (default threshold is 30).
    _force_heartbeat_age(state_root, "claude", age=timedelta(minutes=60))

    janitor = ac.AgentCoordinator("codex", state_root=state_root)
    reclaimed = janitor.reclaim_stale()
    assert "T-2026-05-05-800" in reclaimed
    # Task back in pending.
    assert (state_root / "tasks/pending/T-2026-05-05-800.yaml").exists()
    # Gone from claude's in_progress.
    assert not (
        state_root / "tasks/in_progress/claude/T-2026-05-05-800.yaml"
    ).exists()
    # Reclaim note was appended.
    payload = yaml.safe_load(
        (state_root / "tasks/pending/T-2026-05-05-800.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert any("reclaimed" in n["note"] for n in payload["notes"])


def test_reclaim_stale_skips_fresh_agent(state_root: Path) -> None:
    _write_pending_task(state_root, "T-2026-05-05-810")
    claude = ac.AgentCoordinator("claude", state_root=state_root)
    claude.claim("T-2026-05-05-810")
    claude.emit_heartbeat()  # fresh
    janitor = ac.AgentCoordinator("codex", state_root=state_root)
    reclaimed = janitor.reclaim_stale()
    assert "T-2026-05-05-810" not in reclaimed
    assert (
        state_root / "tasks/in_progress/claude/T-2026-05-05-810.yaml"
    ).exists()


def test_reclaim_stale_handles_missing_heartbeat(state_root: Path) -> None:
    """If an agent has no heartbeat at all, treat as stale."""
    _write_pending_task(state_root, "T-2026-05-05-820")
    claude = ac.AgentCoordinator("claude", state_root=state_root)
    claude.claim("T-2026-05-05-820")
    # Deliberately do NOT emit heartbeat.
    janitor = ac.AgentCoordinator("codex", state_root=state_root)
    reclaimed = janitor.reclaim_stale()
    assert "T-2026-05-05-820" in reclaimed


# --------------------------------------------------------------------------
# Pending-list filtering
# --------------------------------------------------------------------------


def test_list_pending_respects_preferred_agent(state_root: Path) -> None:
    _write_pending_task(state_root, "T-A", preferred_agent="any")
    _write_pending_task(state_root, "T-B", preferred_agent="claude")
    _write_pending_task(state_root, "T-C", preferred_agent="codex")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    visible = {t["id"] for t in c.list_pending(preferred_agent="claude")}
    assert visible == {"T-A", "T-B"}


def test_list_pending_no_filter_returns_all(state_root: Path) -> None:
    _write_pending_task(state_root, "T-A", preferred_agent="any")
    _write_pending_task(state_root, "T-B", preferred_agent="claude")
    _write_pending_task(state_root, "T-C", preferred_agent="codex")
    c = ac.AgentCoordinator("claude", state_root=state_root)
    visible = {t["id"] for t in c.list_pending()}
    assert visible == {"T-A", "T-B", "T-C"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_heartbeat(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    rc = ac.main(["heartbeat", "--agent", "claude"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "heartbeat ok for claude" in out
    assert (state_root / "agents/claude.heartbeat.json").exists()


def test_cli_list_pending(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    _write_pending_task(state_root, "T-CLI-1")
    rc = ac.main(["list-pending"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "T-CLI-1" in out


def test_cli_claim_and_complete(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    _write_pending_task(state_root, "T-CLI-2")
    rc = ac.main(["claim", "--agent", "claude", "--task", "T-CLI-2"])
    assert rc == 0
    rc = ac.main(
        [
            "complete",
            "--agent",
            "claude",
            "--task",
            "T-CLI-2",
            "--deliverable",
            "foo.py",
            "--summary",
            "shipped",
        ]
    )
    assert rc == 0
    assert (state_root / "tasks/completed/T-CLI-2.yaml").exists()


def test_cli_claim_missing_returns_error_code(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    rc = ac.main(["claim", "--agent", "claude", "--task", "T-NOPE"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ERR" in err


def test_cli_claim_respects_preferred_agent_by_default(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    _write_pending_task(state_root, "T-CLI-PREF", preferred_agent="claude")

    rc = ac.main(["claim", "--agent", "codex", "--task", "T-CLI-PREF"])

    assert rc == 2
    assert "preferred_agent=claude" in capsys.readouterr().err
    assert (state_root / "tasks/pending/T-CLI-PREF.yaml").exists()


def test_cli_claim_force_preferred_allows_operator_override(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    _write_pending_task(state_root, "T-CLI-FORCE", preferred_agent="claude")

    rc = ac.main([
        "claim",
        "--agent",
        "codex",
        "--task",
        "T-CLI-FORCE",
        "--force-preferred",
    ])

    assert rc == 0
    assert (state_root / "tasks/in_progress/codex/T-CLI-FORCE.yaml").exists()


def test_cli_block(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    _write_pending_task(state_root, "T-CLI-3")
    ac.main(["claim", "--agent", "claude", "--task", "T-CLI-3"])
    rc = ac.main(
        [
            "block",
            "--agent",
            "claude",
            "--task",
            "T-CLI-3",
            "--reason",
            "needs human",
        ]
    )
    assert rc == 0
    assert (state_root / "tasks/blocked/T-CLI-3.yaml").exists()


def test_cli_reclaim_stale(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(ac, "DEFAULT_STATE_ROOT", state_root)
    _write_pending_task(state_root, "T-CLI-4")
    claude = ac.AgentCoordinator("claude", state_root=state_root)
    claude.claim("T-CLI-4")
    claude.emit_heartbeat()
    _force_heartbeat_age(state_root, "claude", age=timedelta(minutes=120))
    rc = ac.main(["reclaim-stale"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "T-CLI-4" in out

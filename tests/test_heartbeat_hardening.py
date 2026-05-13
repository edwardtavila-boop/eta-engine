"""Tests for the supervisor heartbeat hardening (T-2026-05-05-002-followup).

Codex T-002 canonicalized the heartbeat write path; this followup
hardened the write itself: widened the try/except, added an
independent keep-alive timer thread, and extended the diagnostic CLI
to distinguish ``main_loop_stuck`` from ``supervisor_dead``.

These tests exercise the new behaviour:

* ``_write_heartbeat`` swallows broad exceptions (e.g. ``bot.to_state``
  raising) and logs them to ``heartbeat_write_errors.jsonl`` instead of
  crashing the supervisor.
* ``_HeartbeatKeepAlive`` daemon thread refreshes
  ``heartbeat_keepalive.json`` on its own cadence even when the main
  tick loop is blocked.
* ``_HeartbeatKeepAlive.stop()`` returns promptly so SIGTERM/SIGINT
  shutdowns don't hang.
* ``build_supervisor_heartbeat_report`` reports ``main_loop_stuck``
  when the keep-alive is fresh but the canonical heartbeat is stale.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts.jarvis_strategy_supervisor import (
    BotInstance,
    JarvisStrategySupervisor,
    SupervisorConfig,
    _HeartbeatKeepAlive,
)

# ── _write_heartbeat hardening ────────────────────────────────────


def test_write_heartbeat_swallows_bot_to_state_failure(
    tmp_path: Path,
) -> None:
    """A bot whose ``to_state`` raises must NOT crash the supervisor.

    Codex T-002's narrow ``OSError`` catch let any non-OSError bubble
    out of ``_write_heartbeat`` into the run_forever loop, where the
    supervisor would either crash or stop emitting heartbeats. The
    widened catch converts the failure into a logged ERROR plus a
    structured sidecar entry; the next tick is free to try again.
    """
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    class _BadBot(BotInstance):
        def to_state(self, *, mode: str | None = None) -> dict:
            raise RuntimeError("synthetic to_state failure")

    bad_bot = _BadBot(
        bot_id="bad-bot",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    sup.bots.append(bad_bot)

    # Should NOT raise; should log + write the sidecar.
    sup._write_heartbeat(tick_count=1)

    # Counter incremented.
    assert sup._heartbeat_write_errors == 1

    # Sidecar JSONL was written next to the heartbeat file.
    sidecar = cfg.state_dir / "heartbeat_write_errors.jsonl"
    assert sidecar.exists()
    lines = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["exc_type"] == "RuntimeError"
    assert "synthetic to_state failure" in rec["exc_repr"]
    assert rec["tick_count"] == 1
    assert rec["error_count"] == 1

    # A second failed tick appends — never overwrites.
    sup._write_heartbeat(tick_count=2)
    lines2 = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines2) == 2
    assert lines2[1]["tick_count"] == 2
    assert sup._heartbeat_write_errors == 2


def test_write_heartbeat_propagates_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    """Operator-initiated shutdowns must propagate uncaught.

    KeyboardInterrupt and SystemExit are explicitly excluded from the
    widened catch — if the operator hits Ctrl+C during a heartbeat
    write, the supervisor should exit cleanly, not swallow the
    interrupt and keep ticking.
    """
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    class _InterruptBot(BotInstance):
        def to_state(self, *, mode: str | None = None) -> dict:
            raise KeyboardInterrupt

    sup.bots.append(
        _InterruptBot(
            bot_id="ki",
            symbol="BTC",
            strategy_kind="x",
            direction="long",
            cash=5000.0,
        )
    )

    raised = False
    try:
        sup._write_heartbeat(tick_count=1)
    except KeyboardInterrupt:
        raised = True
    assert raised, "KeyboardInterrupt must propagate"
    # Counter NOT incremented — KI was not a swallowed error.
    assert sup._heartbeat_write_errors == 0


# ── _HeartbeatKeepAlive ───────────────────────────────────────────


def test_keepalive_writes_initial_stamp_synchronously(
    tmp_path: Path,
) -> None:
    """``start()`` must write one stamp synchronously.

    The diagnostic CLI may be invoked immediately after the supervisor
    boots; without a synchronous initial stamp the diagnostic could
    see a missing keepalive even though the thread was scheduled.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    keepalive = _HeartbeatKeepAlive(state_dir=state_dir, period_s=60)
    try:
        keepalive.start()
        # File exists immediately after start, no sleep needed.
        assert keepalive.path.exists()
        payload = json.loads(keepalive.path.read_text(encoding="utf-8"))
        assert "keepalive_ts" in payload
    finally:
        keepalive.stop()


def test_keepalive_timer_writes_when_main_loop_blocks(
    tmp_path: Path,
) -> None:
    """The keep-alive must refresh independently of the main loop.

    Simulates a blocked main loop by holding a synthetic mutex while
    the keep-alive thread runs; confirms the keep-alive file is
    refreshed on its own cadence even though the "main loop" is
    sleeping inside the lock. We compare the payload's ``keepalive_ts``
    field (microsecond resolution) rather than file mtime (which can
    be coarse on Windows FAT/NTFS).
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    # Short period so the test stays fast — sub-second is fine
    # because the floor in __init__ allows it.
    keepalive = _HeartbeatKeepAlive(state_dir=state_dir, period_s=0.05)
    keepalive.start()
    try:
        first_payload = json.loads(keepalive.path.read_text(encoding="utf-8"))
        first_ts = first_payload["keepalive_ts"]

        # Simulate the main loop being stuck — the test thread blocks
        # on a synthetic event for longer than the keepalive period.
        # The keepalive thread is daemon + decoupled, so it must
        # continue refreshing while we sit inside the wait.
        blocked = threading.Event()
        threading.Thread(
            target=lambda: blocked.wait(0.5),
            daemon=True,
            name="fake-blocked-main",
        ).start()
        time.sleep(0.5)

        # Payload's keepalive_ts should have advanced — the keepalive
        # ran while the "main loop" was blocked.
        new_payload = json.loads(keepalive.path.read_text(encoding="utf-8"))
        new_ts = new_payload["keepalive_ts"]
        assert new_ts != first_ts, (
            f"keepalive ts did not advance while main loop was blocked (first={first_ts}, new={new_ts})"
        )
        # And it really is a later timestamp.
        first_dt = datetime.fromisoformat(first_ts)
        new_dt = datetime.fromisoformat(new_ts)
        assert new_dt > first_dt
    finally:
        keepalive.stop()


def test_keepalive_thread_exits_on_stop(tmp_path: Path) -> None:
    """``stop()`` must let the daemon thread exit promptly.

    SIGTERM/SIGINT path drives ``_handle_stop`` which calls
    ``keepalive.stop()``. We verify the thread joins quickly even
    if the period is large — the stop event aborts the wait.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    # Long period: 60s. If stop() joined synchronously on the period,
    # this test would block.
    keepalive = _HeartbeatKeepAlive(state_dir=state_dir, period_s=60)
    keepalive.start()
    thread = keepalive._thread
    assert thread is not None
    assert thread.is_alive()

    t0 = time.monotonic()
    keepalive.stop(timeout=2.0)
    elapsed = time.monotonic() - t0

    assert not thread.is_alive(), "keepalive thread should have exited"
    assert elapsed < 2.5, f"stop() took {elapsed:.2f}s; expected <2.5s"


def test_keepalive_thread_is_daemon(tmp_path: Path) -> None:
    """Daemon threads do not prevent process exit (CLAUDE constraint)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    keepalive = _HeartbeatKeepAlive(state_dir=state_dir, period_s=1)
    try:
        keepalive.start()
        assert keepalive._thread is not None
        assert keepalive._thread.daemon is True
    finally:
        keepalive.stop()


# ── Diagnostic CLI: main_loop_stuck ───────────────────────────────


def _write_main_heartbeat(
    path: Path,
    ts: datetime,
    *,
    tick_count: int = 7,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": ts.isoformat(),
                "tick_count": tick_count,
                "mode": "paper_live",
                "feed": "composite",
                "feed_health": "ok",
                "bots": [{"bot_id": "mnq-alpha"}],
            }
        ),
        encoding="utf-8",
    )


def _write_keepalive(path: Path, ts: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"keepalive_ts": ts.isoformat()}),
        encoding="utf-8",
    )


def test_supervisor_heartbeat_check_reports_main_loop_stuck(
    tmp_path: Path,
) -> None:
    """Fresh keepalive + stale main heartbeat → ``main_loop_stuck``.

    The diagnostic CLI's short-circuit converts this combination into
    the operator-facing signal that the supervisor process is alive
    but its main loop is blocked. Without this, the operator would
    see ``stale`` and reflexively restart the supervisor — losing the
    chance to diagnose the root-cause block (broker reconnect,
    JARVIS deadlock).
    """
    from eta_engine.scripts import supervisor_heartbeat_check

    now = datetime(2026, 5, 5, 6, 30, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"

    # Main heartbeat is 20 minutes old (well past the 10-min default).
    _write_main_heartbeat(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat.json",
        now - timedelta(minutes=20),
    )
    # Keepalive is 5 seconds old — fresh by the 60s default.
    _write_keepalive(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat_keepalive.json",
        now - timedelta(seconds=5),
    )

    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
        threshold_minutes=10,
    )

    assert report["status"] == "main_loop_stuck"
    assert report["diagnosis"] == "main_heartbeat_stale_keepalive_fresh"
    assert report["healthy"] is False
    # Action items must steer the operator to diagnose, not restart.
    assert any("blocking calls" in item for item in report["action_items"])
    # Keep-alive details surfaced for the operator.
    assert report["keepalive"]["fresh"] is True
    assert report["keepalive"]["age_seconds"] is not None


def test_main_loop_stuck_when_main_heartbeat_missing(
    tmp_path: Path,
) -> None:
    """Missing main heartbeat + fresh keepalive → ``main_loop_stuck``.

    A supervisor that booted and started its keepalive thread but
    has not yet written its first main heartbeat (e.g. first tick is
    blocked) is the same situation as a stale main heartbeat.
    """
    from eta_engine.scripts import supervisor_heartbeat_check

    now = datetime(2026, 5, 5, 6, 30, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"

    # No main heartbeat at all.
    _write_keepalive(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat_keepalive.json",
        now - timedelta(seconds=10),
    )

    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
        threshold_minutes=10,
    )

    assert report["status"] == "main_loop_stuck"
    assert report["diagnosis"] == "main_heartbeat_missing_keepalive_fresh"


def test_both_stale_emits_boot_refusal_warning(tmp_path: Path) -> None:
    """Stale heartbeat AND stale keepalive → boot-refusal hint warning.

    When both files are stale, the supervisor process is most likely
    dead OR stuck in the boot-refusal loop. The diagnostic emits a
    warning that points the operator at the kill_switch_latch.json
    file and the operator runbook section that documents the
    clearance procedure.
    """
    from eta_engine.scripts import supervisor_heartbeat_check

    now = datetime(2026, 5, 5, 6, 30, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"

    _write_main_heartbeat(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat.json",
        now - timedelta(minutes=20),
    )
    _write_keepalive(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat_keepalive.json",
        now - timedelta(minutes=20),
    )

    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
        threshold_minutes=10,
    )

    assert report["status"] == "stale"
    assert any("kill_switch_latch.json" in w for w in report["warnings"])
    assert any("Boot-refusal pattern" in w for w in report["warnings"])


def test_fresh_main_and_keepalive_remains_healthy(tmp_path: Path) -> None:
    """Sanity: keepalive doesn't change the healthy-fresh path."""
    from eta_engine.scripts import supervisor_heartbeat_check

    now = datetime(2026, 5, 5, 6, 30, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"
    _write_main_heartbeat(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat.json",
        now - timedelta(seconds=15),
    )
    _write_keepalive(
        state_root / "jarvis_intel" / "supervisor" / "heartbeat_keepalive.json",
        now - timedelta(seconds=5),
    )
    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
        threshold_minutes=10,
    )
    assert report["healthy"] is True
    assert report["status"] == "fresh"
    assert report["keepalive"]["fresh"] is True


# ── Path constant exported under canonical workspace root ────────


def test_keepalive_path_is_under_canonical_state_dir() -> None:
    """The keepalive file must live under the canonical state tree.

    CLAUDE.md hard rule #1: everything writes under
    ``C:\\EvolutionaryTradingAlgo``, never legacy app paths.
    """
    from eta_engine.scripts.workspace_roots import (
        ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH,
        ETA_RUNTIME_STATE_DIR,
    )

    assert ETA_RUNTIME_STATE_DIR in ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH.parents
    assert ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH.name == "heartbeat_keepalive.json"

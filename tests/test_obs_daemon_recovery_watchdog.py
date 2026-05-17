from __future__ import annotations

from eta_engine.obs import daemon_recovery_watchdog as watchdog
from eta_engine.scripts import workspace_roots


def test_heartbeat_paths_for_uses_only_canonical_runtime_state() -> None:
    assert watchdog.heartbeat_paths_for("daemon_heartbeat.json") == [
        workspace_roots.ETA_RUNTIME_STATE_DIR / "daemon_heartbeat.json"
    ]


def test_heartbeat_age_seconds_uses_first_available_heartbeat(tmp_path, monkeypatch) -> None:
    heartbeat = tmp_path / "daemon_heartbeat.json"
    heartbeat.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watchdog, "heartbeat_paths_for", lambda _name: [tmp_path / "missing.json", heartbeat])
    monkeypatch.setattr(watchdog.time, "time", lambda: heartbeat.stat().st_mtime + 42.5)

    spec = watchdog.DaemonSpec(
        name="daemon",
        heartbeat_filename="daemon_heartbeat.json",
        stale_threshold_s=60.0,
        process_match="daemon.py",
    )

    assert watchdog.heartbeat_age_seconds(spec) == 42.5


def test_main_returns_incident_when_stale_daemon_is_killed(monkeypatch) -> None:
    killed: list[tuple[int, bool]] = []
    spec = watchdog.DaemonSpec(
        name="daemon",
        heartbeat_filename="daemon_heartbeat.json",
        stale_threshold_s=60.0,
        process_match="daemon.py",
    )
    monkeypatch.setattr(watchdog, "WATCHED_DAEMONS", [spec])
    monkeypatch.setattr(watchdog, "heartbeat_age_seconds", lambda _spec: 120.0)
    monkeypatch.setattr(watchdog, "find_processes_matching", lambda _pattern: [123])

    def fake_kill(pid: int, *, dry_run: bool) -> bool:
        killed.append((pid, dry_run))
        return True

    monkeypatch.setattr(watchdog, "kill_pid", fake_kill)

    assert watchdog.main(["--dry-run"]) == 1
    assert killed == [(123, True)]


def test_main_returns_zero_when_heartbeats_are_missing_or_fresh(monkeypatch) -> None:
    specs = [
        watchdog.DaemonSpec("missing", "missing.json", 60.0, "missing.py"),
        watchdog.DaemonSpec("fresh", "fresh.json", 60.0, "fresh.py"),
    ]
    monkeypatch.setattr(watchdog, "WATCHED_DAEMONS", specs)
    monkeypatch.setattr(watchdog, "heartbeat_age_seconds", lambda spec: None if spec.name == "missing" else 10.0)
    monkeypatch.setattr(watchdog, "find_processes_matching", lambda _pattern: [])

    assert watchdog.main([]) == 0

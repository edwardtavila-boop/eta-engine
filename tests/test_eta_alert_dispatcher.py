from __future__ import annotations

import json


def test_main_first_run_writes_snapshot_without_events(tmp_path, monkeypatch) -> None:
    from eta_engine.scripts import eta_alert_dispatcher as mod

    state = tmp_path / "state"
    supervisor = state / "jarvis_intel" / "supervisor"
    supervisor.mkdir(parents=True)
    (supervisor / "heartbeat.json").write_text(
        json.dumps(
            {
                "n_bots": 4,
                "tick_count": 12,
                "fm_breaker": {"tripped": False, "spent_today_usd": 10.0, "cap_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )
    (state / "diamond_leaderboard_latest.json").write_text(
        json.dumps({"prop_ready_bots": ["mnq_sage"], "n_prop_ready": 1}),
        encoding="utf-8",
    )
    (state / "diamond_prop_launch_readiness_latest.json").write_text(
        json.dumps({"overall_verdict": "PAPER_READY", "summary": "paper soak ready"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "HEARTBEAT_PATH", supervisor / "heartbeat.json")
    monkeypatch.setattr(mod, "LEADERBOARD_PATH", state / "diamond_leaderboard_latest.json")
    monkeypatch.setattr(mod, "LAUNCH_READINESS_PATH", state / "diamond_prop_launch_readiness_latest.json")
    monkeypatch.setattr(mod, "EVENTS_LOG", state / "eta_events.jsonl")
    monkeypatch.setattr(mod, "SNAPSHOT_PATH", state / "eta_alert_snapshot.json")

    rc = mod.main()

    assert rc == 0
    snapshot = json.loads((state / "eta_alert_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["supervisor_n_bots"] == 4
    assert snapshot["launch_verdict"] == "PAPER_READY"
    assert not (state / "eta_events.jsonl").exists()


def test_main_emits_launch_readiness_flip_event(tmp_path, monkeypatch) -> None:
    from eta_engine.scripts import eta_alert_dispatcher as mod

    state = tmp_path / "state"
    supervisor = state / "jarvis_intel" / "supervisor"
    supervisor.mkdir(parents=True)
    (supervisor / "heartbeat.json").write_text(
        json.dumps(
            {
                "n_bots": 5,
                "tick_count": 13,
                "fm_breaker": {"tripped": False, "spent_today_usd": 10.0, "cap_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )
    (state / "diamond_leaderboard_latest.json").write_text(
        json.dumps({"prop_ready_bots": ["mnq_sage"], "n_prop_ready": 1}),
        encoding="utf-8",
    )
    (state / "diamond_prop_launch_readiness_latest.json").write_text(
        json.dumps({"overall_verdict": "LIVE_READY", "summary": "all gates passed"}),
        encoding="utf-8",
    )
    (state / "eta_alert_snapshot.json").write_text(
        json.dumps(
            {
                "launch_verdict": "PAPER_READY",
                "launch_summary": "paper soak ready",
                "supervisor_n_bots": 5,
                "prop_ready_bots": ["mnq_sage"],
                "fm_breaker_tripped": False,
                "fm_spent_today_usd": 10.0,
                "fm_cap_usd": 100.0,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "HEARTBEAT_PATH", supervisor / "heartbeat.json")
    monkeypatch.setattr(mod, "LEADERBOARD_PATH", state / "diamond_leaderboard_latest.json")
    monkeypatch.setattr(mod, "LAUNCH_READINESS_PATH", state / "diamond_prop_launch_readiness_latest.json")
    monkeypatch.setattr(mod, "EVENTS_LOG", state / "eta_events.jsonl")
    monkeypatch.setattr(mod, "SNAPSHOT_PATH", state / "eta_alert_snapshot.json")

    rc = mod.main()

    assert rc == 0
    events = [
        json.loads(line)
        for line in (state / "eta_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        event["kind"] == "LAUNCH_READINESS_FLIPPED"
        and event["prev"] == "PAPER_READY"
        and event["curr"] == "LIVE_READY"
        for event in events
    )

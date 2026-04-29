from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import refresh_launch_data as mod

ROOT = Path(__file__).resolve().parents[1]


def test_build_plan_refreshes_launch_data_then_republishes_and_verifies() -> None:
    plan = mod.build_plan()
    names = [name for name, _ in plan]
    commands = [" ".join(command) for _, command in plan]

    assert names == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "vix_5m",
        "vix_1m",
        "nq_daily",
        "announce_data_library",
        "paper_live_launch_check",
    ]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol MNQ --timeframe 5m" in commands[0]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol MNQ --timeframe 1h" in commands[1]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol MNQ --timeframe 4h" in commands[2]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol NQ --timeframe 5m" in commands[3]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol NQ --timeframe 1h" in commands[4]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol NQ --timeframe 4h" in commands[5]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol ES --timeframe 5m" in commands[6]
    assert "eta_engine.scripts.fetch_market_context_bars --symbol DXY --timeframe 5m" in commands[7]
    assert "eta_engine.scripts.fetch_market_context_bars --symbol VIX --timeframe 5m" in commands[8]
    assert "eta_engine.scripts.fetch_market_context_bars --symbol VIX --timeframe 1m" in commands[9]
    assert "eta_engine.scripts.extend_nq_daily_yahoo" in commands[10]
    assert "eta_engine.scripts.announce_data_library" in commands[11]
    assert "eta_engine.scripts.paper_live_launch_check --json" in commands[12]


def test_build_plan_can_skip_inventory_and_verify() -> None:
    plan = mod.build_plan(skip_inventory=True, skip_verify=True)

    assert [name for name, _ in plan] == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "vix_5m",
        "vix_1m",
        "nq_daily",
    ]


def test_run_plan_stops_on_first_failed_step(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_step(name: str, command: list[str]) -> mod.StepResult:
        calls.append(name)
        return mod.StepResult(
            name=name,
            command=command,
            returncode=2 if name == "nq_5m" else 0,
            stdout_tail=f"{name} stdout",
            stderr_tail="",
        )

    monkeypatch.setattr(mod, "run_step", fake_run_step)

    summary = mod.run_plan()

    assert summary["ok"] is False
    assert calls == ["mnq_5m", "mnq_1h", "mnq_4h", "nq_5m"]
    steps = summary["steps"]
    assert len(steps) == 4
    assert steps[-1]["name"] == "nq_5m"
    assert steps[-1]["ok"] is False


def test_run_plan_reports_success(monkeypatch) -> None:
    def fake_run_step(name: str, command: list[str]) -> mod.StepResult:
        return mod.StepResult(
            name=name,
            command=command,
            returncode=0,
            stdout_tail=f"{name} ok",
            stderr_tail="",
        )

    monkeypatch.setattr(mod, "run_step", fake_run_step)

    summary = mod.run_plan(skip_inventory=True, skip_verify=True)

    assert summary["ok"] is True
    assert [step["name"] for step in summary["steps"]] == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "vix_5m",
        "vix_1m",
        "nq_daily",
    ]


def test_live_launch_runbook_mentions_operator_refresh_entrypoint() -> None:
    text = (ROOT / "docs" / "live_launch_runbook.md").read_text(encoding="utf-8")

    assert "python -m eta_engine.scripts.operator_env_bootstrap --create --json" in text
    assert "python -m eta_engine.scripts.refresh_launch_data --json" in text
    assert "DXY` 5m context bars" in text
    assert "VIX` 5m and 1m context bars" in text

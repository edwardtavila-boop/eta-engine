from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import refresh_launch_data as mod

ROOT = Path(__file__).resolve().parents[1]


def _names(plan: list[mod.PlanStep]) -> list[str]:
    return [step.name for step in plan]


def _commands(plan: list[mod.PlanStep]) -> list[str]:
    return [" ".join(step.command) for step in plan]


def test_build_plan_refreshes_launch_data_then_republishes_and_verifies() -> None:
    plan = mod.build_plan()
    names = _names(plan)
    commands = _commands(plan)

    assert names == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "dxy_1h",
        "vix_5m",
        "vix_1m",
        "nq_daily",
        "fear_greed_macro",
        "sol_onchain",
        "announce_data_library",
        "bot_strategy_readiness_snapshot",
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
    assert "eta_engine.scripts.fetch_market_context_bars --symbol DXY --timeframe 1h" in commands[8]
    assert "eta_engine.scripts.fetch_market_context_bars --symbol VIX --timeframe 5m" in commands[9]
    assert "eta_engine.scripts.fetch_market_context_bars --symbol VIX --timeframe 1m" in commands[10]
    assert "eta_engine.scripts.extend_nq_daily_yahoo" in commands[11]
    assert "eta_engine.scripts.fetch_fear_greed_alternative" in commands[12]
    assert "eta_engine.scripts.fetch_onchain_history --symbol SOL" in commands[13]
    assert "eta_engine.scripts.announce_data_library" in commands[14]
    assert "eta_engine.scripts.bot_strategy_readiness --scope supervisor_pinned --snapshot" in commands[15]
    assert "eta_engine.scripts.paper_live_launch_check --scope supervisor_pinned --json --snapshot" in commands[16]
    assert plan[12].required is False
    assert plan[13].required is False


def test_build_plan_can_skip_inventory_and_verify() -> None:
    plan = mod.build_plan(skip_inventory=True, skip_verify=True)

    assert _names(plan) == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "dxy_1h",
        "vix_5m",
        "vix_1m",
        "nq_daily",
        "fear_greed_macro",
        "sol_onchain",
    ]


def test_build_plan_can_skip_optional_steps() -> None:
    plan = mod.build_plan(skip_inventory=True, skip_verify=True, skip_optional=True)

    assert _names(plan) == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "dxy_1h",
        "vix_5m",
        "vix_1m",
        "nq_daily",
    ]


def test_run_plan_stops_on_first_failed_step(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_step(step: mod.PlanStep) -> mod.StepResult:
        calls.append(step.name)
        return mod.StepResult(
            name=step.name,
            command=step.command,
            required=step.required,
            returncode=2 if step.name == "nq_5m" else 0,
            stdout_tail=f"{step.name} stdout",
            stderr_tail="",
        )

    monkeypatch.setattr(mod, "run_step", fake_run_step)

    summary = mod.run_plan()

    assert summary["ok"] is False
    assert summary["failed_required"] == ["nq_5m"]
    assert summary["failed_optional"] == []
    assert calls == ["mnq_5m", "mnq_1h", "mnq_4h", "nq_5m"]
    steps = summary["steps"]
    assert len(steps) == 4
    assert steps[-1]["name"] == "nq_5m"
    assert steps[-1]["ok"] is False


def test_run_plan_continues_after_optional_failed_step(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_step(step: mod.PlanStep) -> mod.StepResult:
        calls.append(step.name)
        return mod.StepResult(
            name=step.name,
            command=step.command,
            required=step.required,
            returncode=2 if step.name == "fear_greed_macro" else 0,
            stdout_tail=f"{step.name} stdout",
            stderr_tail="",
        )

    monkeypatch.setattr(mod, "run_step", fake_run_step)

    summary = mod.run_plan()

    assert summary["ok"] is True
    assert summary["failed_required"] == []
    assert summary["failed_optional"] == ["fear_greed_macro"]
    assert "fear_greed_macro" in calls
    assert calls[-1] == "paper_live_launch_check"
    optional_step = next(step for step in summary["steps"] if step["name"] == "fear_greed_macro")
    assert optional_step["required"] is False
    assert optional_step["ok"] is False


def test_run_plan_reports_success(monkeypatch) -> None:
    def fake_run_step(step: mod.PlanStep) -> mod.StepResult:
        return mod.StepResult(
            name=step.name,
            command=step.command,
            required=step.required,
            returncode=0,
            stdout_tail=f"{step.name} ok",
            stderr_tail="",
        )

    monkeypatch.setattr(mod, "run_step", fake_run_step)

    summary = mod.run_plan(skip_inventory=True, skip_verify=True)

    assert summary["ok"] is True
    assert summary["failed_required"] == []
    assert summary["failed_optional"] == []
    assert [step["name"] for step in summary["steps"]] == [
        "mnq_5m",
        "mnq_1h",
        "mnq_4h",
        "nq_5m",
        "nq_1h",
        "nq_4h",
        "es_5m",
        "dxy_5m",
        "dxy_1h",
        "vix_5m",
        "vix_1m",
        "nq_daily",
        "fear_greed_macro",
        "sol_onchain",
    ]


def test_live_launch_runbook_mentions_operator_refresh_entrypoint() -> None:
    text = (ROOT / "docs" / "live_launch_runbook.md").read_text(encoding="utf-8")

    assert "python -m eta_engine.scripts.operator_env_bootstrap --create --json" in text
    assert "python -m eta_engine.scripts.refresh_launch_data --json" in text
    assert "`failed_required`" in text
    assert "`failed_optional`" in text
    assert "DXY` 5m and 1h context bars" in text
    assert "VIX` 5m and 1m context bars" in text
    assert "advisory optional feed refreshes" in text


def test_readiness_log_mentions_resolution_metadata() -> None:
    text = (ROOT / "docs" / "research_log" / "paper_live_launch_readiness_20260427.md").read_text(encoding="utf-8")

    assert "`resolution.mode`" in text
    assert "proxy" in text
    assert "synthetic" in text

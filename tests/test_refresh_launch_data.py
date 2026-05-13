from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import refresh_launch_data as mod

ROOT = Path(__file__).resolve().parents[1]


def _names(plan: list[mod.PlanStep]) -> list[str]:
    return [step.name for step in plan]


def _commands(plan: list[mod.PlanStep]) -> list[str]:
    return [" ".join(step.command) for step in plan]


_CORE_LAUNCH_STEPS = (
    "mnq_5m", "mnq_1h", "mnq_4h",
    "nq_5m", "nq_1h", "nq_4h",
    "es_5m",
    "dxy_5m", "dxy_1h",
    "vix_5m", "vix_1m",
    "nq_daily",
)

_ACTIVE_FLEET_STEPS = (
    # Wave-25 (2026-05-13): CME crypto micros + commodities + micros
    "fleet_mbt_5m", "fleet_mbt_1h", "fleet_mbt_1d",
    "fleet_met_5m", "fleet_met_1h", "fleet_met_1d",
    "fleet_m2k_5m", "fleet_m2k_1h",
    "fleet_mym_5m", "fleet_mym_1h",
    "fleet_ym_5m", "fleet_ym_1h",
    "fleet_mes_5m", "fleet_mes_1h",
    "fleet_gc_5m", "fleet_gc_1h", "fleet_gc_1d",
    "fleet_mgc_5m", "fleet_mgc_1h",
    "fleet_cl_5m", "fleet_cl_1h", "fleet_cl_1d",
    "fleet_mcl_5m", "fleet_mcl_1h",
    "fleet_ng_5m", "fleet_ng_1h", "fleet_ng_1d",
    "fleet_6e_5m", "fleet_6e_1h",
    "fleet_zn_5m", "fleet_zn_1h", "fleet_zn_1d",
    "fleet_mnq_1d",
    "vix_1h",
)

_OPTIONAL_STEPS = (
    "fear_greed_macro",
    "sol_onchain",
    "btc_onchain",
    "eth_onchain",
)


def test_build_plan_refreshes_launch_data_then_republishes_and_verifies() -> None:
    """Plan must include core launch + active-fleet + optional + tail steps."""
    plan = mod.build_plan()
    names = _names(plan)
    commands = _commands(plan)

    # Every named step must be present (no asserting EXACT order so the
    # active-fleet wave can grow without breaking this test).
    for step in (*_CORE_LAUNCH_STEPS, *_ACTIVE_FLEET_STEPS, *_OPTIONAL_STEPS):
        assert step in names, f"missing plan step: {step}"
    # Inventory + verify tail steps
    for tail_step in ("announce_data_library", "bot_strategy_readiness_snapshot", "paper_live_launch_check"):
        assert tail_step in names, f"missing tail step: {tail_step}"

    # Core MNQ/NQ/ES/DXY/VIX commands still anchor the head of the plan
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol MNQ --timeframe 5m" in commands[0]
    assert "eta_engine.scripts.fetch_index_futures_bars --symbol NQ --timeframe 5m" in commands[3]

    # Active-fleet steps must use fetch_index_futures_bars
    fleet_cmds = [c for n, c in zip(names, commands, strict=False) if n.startswith("fleet_")]
    assert all("fetch_index_futures_bars" in c for c in fleet_cmds), (
        "every fleet_* step must invoke fetch_index_futures_bars"
    )

    # Optional steps must be marked required=False
    name_to_step = {s.name: s for s in plan}
    for opt in _OPTIONAL_STEPS:
        assert name_to_step[opt].required is False, f"{opt} should be optional"


def test_build_plan_can_skip_inventory_and_verify() -> None:
    plan = mod.build_plan(skip_inventory=True, skip_verify=True)
    names = set(_names(plan))

    # Core + fleet + optional all present
    for step in (*_CORE_LAUNCH_STEPS, *_ACTIVE_FLEET_STEPS, *_OPTIONAL_STEPS):
        assert step in names
    # Tail steps absent
    for tail in ("announce_data_library", "bot_strategy_readiness_snapshot", "paper_live_launch_check"):
        assert tail not in names


def test_build_plan_can_skip_optional_steps() -> None:
    plan = mod.build_plan(skip_inventory=True, skip_verify=True, skip_optional=True)
    names = set(_names(plan))

    # Core + fleet present
    for step in (*_CORE_LAUNCH_STEPS, *_ACTIVE_FLEET_STEPS):
        assert step in names
    # Optional + tail absent
    for opt in _OPTIONAL_STEPS:
        assert opt not in names, f"{opt} should be skipped with skip_optional=True"
    for tail in ("announce_data_library", "bot_strategy_readiness_snapshot", "paper_live_launch_check"):
        assert tail not in names


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
    step_names = {step["name"] for step in summary["steps"]}
    # Core + fleet + optional present
    for step in (*_CORE_LAUNCH_STEPS, *_ACTIVE_FLEET_STEPS, *_OPTIONAL_STEPS):
        assert step in step_names, f"missing step: {step}"


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

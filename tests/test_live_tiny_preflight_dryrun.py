"""Tests for scripts.live_tiny_preflight_dryrun."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts import live_tiny_preflight_dryrun as mod

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "configs").mkdir()
    # kill_log
    (tmp_path / "docs" / "kill_log.json").write_text(
        json.dumps({"meta": {}, "entries": [{"id": 1}, {"id": 2}, {"id": 3}]}),
    )
    # paper_run_report with Tier-A passing
    (tmp_path / "docs" / "paper_run_report.json").write_text(
        json.dumps(
            {
                "per_bot": [
                    {"bot": "mnq", "gate_pass": True},
                    {"bot": "nq", "gate_pass": True},
                    {"bot": "eth_perp", "gate_pass": False},
                ],
            }
        ),
    )
    # roadmap_state
    (tmp_path / "roadmap_state.json").write_text(
        json.dumps(
            {
                "current_phase": "P9_ROLLOUT",
                "overall_progress_pct": 72,
                "shared_artifacts": {
                    "firm_board_latest": {
                        "spec_id": "APEX_PAPER_RESULTS_v1",
                        "final_verdict": "GO",
                    },
                },
            }
        )
    )
    # firm_spec_paper_promotion_v1 with healthy risk envelope
    (tmp_path / "docs" / "firm_spec_paper_promotion_v1.json").write_text(
        json.dumps(
            {
                "risk_management": {
                    "per_trade_risk_pct": 3.0,
                    "daily_loss_cap_pct": 6.0,
                    "max_drawdown_kill_pct": 20.0,
                    "paper_capital_allocations": {"mnq": 5000, "nq": 12000},
                },
            }
        )
    )
    return tmp_path


def test_gate_kill_log_passes(fake_root: Path):
    g = mod._gate_kill_log()
    assert g.status == "PASS"
    assert g.evidence["count"] == 3


def test_gate_kill_log_fails_when_missing(fake_root: Path):
    (fake_root / "docs" / "kill_log.json").unlink()
    g = mod._gate_kill_log()
    assert g.status == "FAIL"


def test_gate_paper_run_tier_a_pass(fake_root: Path):
    g = mod._gate_paper_run()
    assert g.status == "PASS"
    assert g.evidence["passes"]["mnq"] is True


def test_gate_paper_run_tier_a_fail_if_any_tier_a_fails(fake_root: Path):
    (fake_root / "docs" / "paper_run_report.json").write_text(
        json.dumps(
            {
                "per_bot": [
                    {"bot": "mnq", "gate_pass": True},
                    {"bot": "nq", "gate_pass": False},
                ],
            }
        ),
    )
    g = mod._gate_paper_run()
    assert g.status == "FAIL"


def test_gate_firm_verdict_pass(fake_root: Path):
    g = mod._gate_firm_verdict()
    assert g.status == "PASS"


def test_gate_firm_verdict_fail_on_modify(fake_root: Path):
    rs = json.loads((fake_root / "roadmap_state.json").read_text())
    rs["shared_artifacts"]["firm_board_latest"]["final_verdict"] = "MODIFY"
    (fake_root / "roadmap_state.json").write_text(json.dumps(rs))
    g = mod._gate_firm_verdict()
    assert g.status == "FAIL"


def test_gate_roadmap_state_pass(fake_root: Path):
    g = mod._gate_roadmap_state()
    assert g.status == "PASS"
    assert "P9" in g.detail


def test_gate_roadmap_state_fail_on_wrong_phase(fake_root: Path):
    rs = json.loads((fake_root / "roadmap_state.json").read_text())
    rs["current_phase"] = "P3_PROOF"
    (fake_root / "roadmap_state.json").write_text(json.dumps(rs))
    g = mod._gate_roadmap_state()
    assert g.status == "FAIL"


def test_gate_tradovate_creds_reads_env(fake_root: Path, monkeypatch: pytest.MonkeyPatch):
    # Post-dormancy mandate (2026-04-24): Tradovate is DORMANT, so
    # missing creds resolve to SKIP rather than FAIL. The gate is
    # ``required=False`` so the missing-creds path no longer blocks
    # the live-tiny staging flow.
    monkeypatch.delenv("TRADOVATE_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRADOVATE_CLIENT_SECRET", raising=False)
    g = mod._gate_tradovate_creds()
    assert g.status == "SKIP"
    monkeypatch.setenv("TRADOVATE_CLIENT_ID", "x")
    monkeypatch.setenv("TRADOVATE_CLIENT_SECRET", "y")
    g = mod._gate_tradovate_creds()
    assert g.status == "PASS"


def test_gate_risk_sizing_pass(fake_root: Path):
    g = mod._gate_risk_sizing()
    assert g.status == "PASS"
    assert g.evidence["tier_A_capital_usd"] == 17_000


def test_gate_risk_sizing_fails_on_huge_per_trade(fake_root: Path):
    spec = json.loads((fake_root / "docs" / "firm_spec_paper_promotion_v1.json").read_text())
    spec["risk_management"]["per_trade_risk_pct"] = 10.0
    (fake_root / "docs" / "firm_spec_paper_promotion_v1.json").write_text(json.dumps(spec))
    g = mod._gate_risk_sizing()
    assert g.status == "FAIL"


def test_abort_on_red_loop_triggers_when_required_fail(fake_root: Path):
    gates = [
        mod.Gate(name="kill_log_present", required=True, status="FAIL", detail=""),
        mod.Gate(name="pytest_green", required=True, status="PASS", detail=""),
        mod.Gate(name="tradovate_creds", required=False, status="FAIL", detail=""),
    ]
    g = mod._gate_abort_on_red_loop(gates)
    assert g.status == "PASS"
    assert "kill_log_present" in g.detail


def test_abort_on_red_loop_idle_when_all_required_green(fake_root: Path):
    gates = [
        mod.Gate(name="x", required=True, status="PASS", detail=""),
        mod.Gate(name="y", required=True, status="PASS", detail=""),
        mod.Gate(name="z", required=False, status="FAIL", detail=""),
    ]
    g = mod._gate_abort_on_red_loop(gates)
    assert g.status == "PASS"
    assert "idle" in g.detail.lower() or "no required" in g.detail.lower()


def test_inject_failures_flips_gate_state(fake_root: Path):
    gates = [mod.Gate(name="foo", required=True, status="PASS", detail="ok")]
    mod._inject_failures(gates, {"foo"})
    assert gates[0].status == "FAIL"
    assert "injected" in gates[0].detail


def test_gate_venue_health_pass_with_all_configs(fake_root: Path):
    # Post-dormancy mandate (2026-04-24): active futures venues are
    # IBKR + Tastytrade; Tradovate yaml is no longer required. The
    # gate derives required configs from
    # ``venues.router.ACTIVE_FUTURES_VENUES`` so the test mirrors that.
    cfg = fake_root / "configs"
    for fname in (
        "ibkr.yaml",
        "tastytrade.yaml",
        "bybit.yaml",
        "alerts.yaml",
        "kill_switch.yaml",
    ):
        (cfg / fname).write_text(f"# stub {fname}\n")
    g = mod._gate_venue_health()
    assert g.status == "PASS"
    assert "5/5" in g.detail


def test_gate_venue_health_fail_if_any_config_missing(fake_root: Path):
    cfg = fake_root / "configs"
    # write only 2 of 4
    (cfg / "tradovate.yaml").write_text("# stub")
    (cfg / "bybit.yaml").write_text("# stub")
    g = mod._gate_venue_health()
    assert g.status == "FAIL"
    assert "missing" in g.detail.lower()


def test_gate_decisions_locked_pass(fake_root: Path):
    p = fake_root / "docs" / "decisions_v1.json"
    p.write_text(
        json.dumps(
            {
                "spec_id": "APEX_DECISIONS_v1",
                "tier_1_live_tiny_blockers": {"x": 1},
                "tier_2_tier_b_blockers": {"x": 1},
                "tier_3_operational_cadence": {"x": 1},
            }
        )
    )
    g = mod._gate_decisions_locked()
    assert g.status == "PASS"
    assert "APEX_DECISIONS_v1" in g.detail


def test_gate_decisions_locked_fail_on_missing_section(fake_root: Path):
    p = fake_root / "docs" / "decisions_v1.json"
    # missing tier_3
    p.write_text(
        json.dumps(
            {
                "spec_id": "APEX_DECISIONS_v1",
                "tier_1_live_tiny_blockers": {},
                "tier_2_tier_b_blockers": {},
            }
        )
    )
    g = mod._gate_decisions_locked()
    assert g.status == "FAIL"


def test_gate_decisions_locked_fail_if_missing_file(fake_root: Path):
    g = mod._gate_decisions_locked()
    assert g.status == "FAIL"


def test_gate_env_template_pass(fake_root: Path):
    p = fake_root / ".env.example"
    p.write_text(
        "\n".join(
            [
                "TRADOVATE_CLIENT_ID=",
                "TRADOVATE_CLIENT_SECRET=",
                "TRADOVATE_USERNAME=",
                "TRADOVATE_PASSWORD=",
                "TRADOVATE_DEVICE_ID=",
                "BYBIT_API_KEY=",
                "BYBIT_API_SECRET=",
                "PUSHOVER_USER=",
                "PUSHOVER_TOKEN=",
            ]
        )
    )
    g = mod._gate_env_template()
    assert g.status == "PASS"


def test_gate_env_template_fail_on_missing_key(fake_root: Path):
    p = fake_root / ".env.example"
    p.write_text("TRADOVATE_CLIENT_ID=\n")  # missing most keys
    g = mod._gate_env_template()
    assert g.status == "FAIL"


def test_gate_go_trigger_armed_pass(fake_root: Path):
    scripts = fake_root / "scripts"
    scripts.mkdir()
    (scripts / "go_trigger.py").write_text("# stub")
    (scripts / "schedule_weekly_review.py").write_text("# stub")
    g = mod._gate_go_trigger_armed()
    assert g.status == "PASS"


def test_gate_go_trigger_armed_fail_if_missing(fake_root: Path):
    g = mod._gate_go_trigger_armed()
    assert g.status == "FAIL"


# --------------------------------------------------------------------------- #
# runtime_wired gate
# --------------------------------------------------------------------------- #
def test_gate_runtime_wired_passes_with_real_tree():
    """Invoke against the live eta_engine tree (no monkeypatch) so we
    actually exercise the import smoke check."""
    g = mod._gate_runtime_wired()
    assert g.status == "PASS", g.detail
    assert "importable" in g.detail


def test_gate_runtime_wired_fails_when_module_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    # Only create one of the three — expect FAIL.
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "kill_switch_runtime.py").write_text("# stub")
    g = mod._gate_runtime_wired()
    assert g.status == "FAIL"
    assert "missing" in g.detail.lower()

"""
EVOLUTIONARY TRADING ALGO  //  tests.test_paper_run_harness
===============================================
Unit + smoke coverage for the paper-run harness and paper-phase gate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts import paper_run_harness as prh

if TYPE_CHECKING:
    from pathlib import Path

# ── Gate unit tests ──


def _make_result(**kw) -> prh.BotPaperResult:
    base = dict(
        bot="mnq",
        symbol="MNQ",
        n_trades=50,
        win_rate=0.60,
        expectancy_r=0.45,
        avg_win_r=1.5,
        avg_loss_r=1.0,
        profit_factor=2.0,
        max_dd_pct=8.0,
        total_return_pct=25.0,
        final_equity_usd=6_250.0,
        starting_equity_usd=5_000.0,
        sharpe=1.2,
        sortino=1.6,
        killed=False,
        kill_events=0,
        telemetry_gaps=0,
    )
    base.update(kw)
    return prh.BotPaperResult(**base)


def test_gate_passes_on_healthy_result() -> None:
    reqs = prh.PaperPhaseRequirements()
    r = _make_result()
    prh._apply_gate(r, reqs, weeks=4)
    assert r.gate_pass is True
    assert r.gate_failures == []


def test_gate_fails_on_insufficient_trades() -> None:
    reqs = prh.PaperPhaseRequirements()
    r = _make_result(n_trades=10)
    prh._apply_gate(r, reqs, weeks=4)
    assert r.gate_pass is False
    assert any("n_trades" in f for f in r.gate_failures)


def test_gate_fails_on_low_expectancy() -> None:
    reqs = prh.PaperPhaseRequirements()
    r = _make_result(expectancy_r=0.10)
    prh._apply_gate(r, reqs, weeks=4)
    assert r.gate_pass is False
    assert any("expectancy_r" in f for f in r.gate_failures)


def test_gate_fails_on_dd_breach() -> None:
    reqs = prh.PaperPhaseRequirements()
    r = _make_result(max_dd_pct=22.0)
    prh._apply_gate(r, reqs, weeks=4)
    assert r.gate_pass is False
    assert any("max_dd_pct" in f for f in r.gate_failures)


def test_gate_fails_on_telemetry_gap() -> None:
    reqs = prh.PaperPhaseRequirements()
    r = _make_result(telemetry_gaps=3)
    prh._apply_gate(r, reqs, weeks=4)
    assert r.gate_pass is False
    assert any("telemetry_gaps" in f for f in r.gate_failures)


def test_gate_fails_on_too_few_weeks() -> None:
    reqs = prh.PaperPhaseRequirements()
    r = _make_result()
    prh._apply_gate(r, reqs, weeks=2)
    assert r.gate_pass is False
    assert any("min_weeks" in f for f in r.gate_failures)


def test_gate_fails_on_kill_switch_rate() -> None:
    reqs = prh.PaperPhaseRequirements()
    # 2 kill events in 4 weeks ~ 2/0.93 mo > 1/mo threshold
    r = _make_result(killed=True, kill_events=2)
    prh._apply_gate(r, reqs, weeks=4)
    assert r.gate_pass is False
    assert any("kill_switch_rate" in f for f in r.gate_failures)


# ── Aggregate ──


def test_aggregate_all_pass_emits_go() -> None:
    reqs = prh.PaperPhaseRequirements()
    rs = [
        _make_result(bot="mnq", starting_equity_usd=5_000.0, final_equity_usd=6_000.0),
        _make_result(bot="nq", starting_equity_usd=12_000.0, final_equity_usd=13_500.0),
    ]
    for r in rs:
        prh._apply_gate(r, reqs, weeks=4)
    agg = prh._aggregate(rs, reqs, weeks=4)
    assert agg.promotion_verdict == "GO"
    assert agg.bots_gate_pass == 2
    assert agg.total_bots == 2


def test_aggregate_partial_fail_emits_modify() -> None:
    reqs = prh.PaperPhaseRequirements()
    rs = [
        _make_result(bot="mnq"),
        _make_result(bot="nq", expectancy_r=0.05),
    ]
    for r in rs:
        prh._apply_gate(r, reqs, weeks=4)
    agg = prh._aggregate(rs, reqs, weeks=4)
    assert agg.promotion_verdict == "MODIFY"
    assert agg.bots_gate_pass == 1
    assert "nq" in agg.verdict_reason


def test_aggregate_kill_event_emits_modify_not_go() -> None:
    reqs = prh.PaperPhaseRequirements()
    rs = [
        _make_result(bot="mnq", killed=True, kill_events=1, max_dd_pct=22.0),
    ]
    for r in rs:
        prh._apply_gate(r, reqs, weeks=4)
    agg = prh._aggregate(rs, reqs, weeks=4)
    # killed + failed gate -> MODIFY
    assert agg.promotion_verdict == "MODIFY"
    assert agg.any_killed is True


# ── End-to-end smoke ──


def test_harness_runs_one_bot(tmp_path: Path) -> None:
    plan = prh.BOT_PLAN["mnq"]
    reqs = prh.PaperPhaseRequirements()
    r = prh._run_one_bot("mnq", plan, weeks=4, seed=11, reqs=reqs)
    assert r.bot == "mnq"
    assert r.symbol == "MNQ"
    assert r.starting_equity_usd == plan["capital"]
    assert r.n_trades >= 0
    assert -1.0 <= r.win_rate <= 1.0
    assert r.max_dd_pct >= 0.0


def test_harness_produces_report_and_tearsheet(tmp_path: Path) -> None:
    reqs = prh.PaperPhaseRequirements()
    # Run 2 bots for speed
    plan_subset = {k: v for k, v in list(prh.BOT_PLAN.items())[:2]}
    results = []
    for i, (bot, plan) in enumerate(plan_subset.items()):
        r = prh._run_one_bot(bot, plan, weeks=4, seed=11 + i * 7, reqs=reqs)
        results.append(r)
    agg = prh._aggregate(results, reqs, weeks=4)
    report_path, tearsheet_path = prh._write_report(
        results,
        agg,
        weeks=4,
        seed=11,
        reqs=reqs,
        out_dir=tmp_path,
    )
    assert report_path.exists()
    assert tearsheet_path.exists()
    data = json.loads(report_path.read_text())
    assert data["kind"] == "apex_paper_run_report"
    assert data["weeks"] == 4
    assert len(data["per_bot"]) == 2
    assert "promotion_verdict" in data["aggregate"]
    txt = tearsheet_path.read_text()
    assert "EVOLUTIONARY TRADING ALGO -- Paper Run Tearsheet" in txt
    assert "Aggregate" in txt


def test_bot_plan_matches_spec_capital_allocations() -> None:
    # Sanity: per-bot capital aligns with firm_spec_paper_promotion_v1 allocations
    expected = {
        "mnq": 5_000.0,
        "nq": 12_000.0,
        "crypto_seed": 2_000.0,
        "eth_perp": 3_000.0,
        "sol_perp": 3_000.0,
        "xrp_perp": 2_000.0,
    }
    for k, v in expected.items():
        assert prh.BOT_PLAN[k]["capital"] == pytest.approx(v)
    total = sum(p["capital"] for p in prh.BOT_PLAN.values())
    assert total == pytest.approx(27_000.0)

"""Tests for the futures prop-lane ladder automation."""

from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.scripts import futures_prop_ladder as ladder
from eta_engine.scripts import workspace_roots


def _row(
    bot_id: str,
    symbol: str,
    *,
    launch_lane: str = "paper_soak",
    can_live_trade: bool = False,
    promotion_status: str = "production_candidate",
) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "symbol": symbol,
        "strategy_kind": "confluence_scorecard",
        "launch_lane": launch_lane,
        "can_paper_trade": True,
        "can_live_trade": can_live_trade,
        "promotion_status": promotion_status,
        "missing_critical": [],
    }


def test_build_ladder_keeps_volume_profile_mnq_primary_and_reserves_three_runners() -> None:
    report = ladder.build_ladder_report(
        readiness_rows=[
            _row("sol_optimized", "SOL"),
            _row("volume_profile_nq", "NQ1"),
            _row("rsi_mr_mnq_v2", "MNQ1", promotion_status="paper_soak"),
            _row("mym_sweep_reclaim", "MYM1", promotion_status="paper_soak"),
            _row("mes_sweep_reclaim_v2", "MES1", promotion_status="research_candidate"),
            _row("mnq_anchor_sweep", "MNQ1", promotion_status="paper_soak"),
            _row("volume_profile_mnq", "MNQ1"),
        ],
        strict_gate_metrics={
            "volume_profile_mnq": {"trades": 2916, "sh_def": 2.86, "L": True, "S": True},
            "volume_profile_nq": {"trades": 3073, "sh_def": 2.08, "L": True, "S": False},
            "rsi_mr_mnq_v2": {"trades": 285, "sh_def": -0.22, "L": True, "S": False},
            "mym_sweep_reclaim": {"trades": 11, "sh_def": -0.16, "L": True, "S": False},
            "mes_sweep_reclaim_v2": {"trades": 23, "sh_def": -0.52, "L": True, "S": False},
        },
        prop_readiness={"summary": "READY_FOR_DEPOSIT"},
    )

    assert report["summary"]["primary_bot"] == "volume_profile_mnq"
    assert report["summary"]["runner_slots"] == 3
    assert report["summary"]["automation_mode"] == "FULLY_AUTOMATED_PAPER_PROP_HELD"
    assert [candidate["bot_id"] for candidate in report["candidates"][:4]] == [
        "volume_profile_mnq",
        "volume_profile_nq",
        "rsi_mr_mnq_v2",
        "mym_sweep_reclaim",
    ]
    assert report["candidates"][0]["role"] == "primary"
    assert report["candidates"][0]["evidence_grade"] == "strict_pass"
    assert all(candidate["role"] != "primary" for candidate in report["candidates"][1:])
    assert all(candidate["live_routing_allowed"] is False for candidate in report["candidates"])


def test_ladder_blocks_cutover_until_prop_and_live_gates_are_green() -> None:
    report = ladder.build_ladder_report(
        readiness_rows=[
            _row("volume_profile_mnq", "MNQ1", can_live_trade=False),
            _row("volume_profile_nq", "NQ1", can_live_trade=True),
        ],
        strict_gate_metrics={"volume_profile_mnq": {"trades": 2916, "sh_def": 2.86, "L": True, "S": True}},
        prop_readiness={"summary": "READY_FOR_DRY_RUN"},
    )

    primary = report["candidates"][0]
    assert primary["bot_id"] == "volume_profile_mnq"
    assert primary["live_routing_allowed"] is False
    assert "bot row is not can_live_trade" in primary["blockers"]
    assert report["summary"]["automation_mode"] == "PROP_DRY_RUN_READY_LIVE_BLOCKED"


def test_ladder_treats_kaizen_retired_primary_as_quarantined_not_stale() -> None:
    report = ladder.build_ladder_report(
        readiness_rows=[
            {
                **_row("volume_profile_mnq", "MNQ1", launch_lane="deactivated", promotion_status="deactivated"),
                "active": False,
                "data_status": "deactivated",
                "deactivation_source": "kaizen_sidecar",
                "deactivation_reason": "tier=DECAY mc=MIXED expR=-0.0061 n=66",
            },
            _row("volume_profile_nq", "NQ1", can_live_trade=True),
        ],
        strict_gate_metrics={"volume_profile_mnq": {"trades": 2916, "sh_def": 2.86, "L": True, "S": True}},
        prop_readiness={"summary": "READY_FOR_DRY_RUN"},
    )

    primary = report["candidates"][0]
    actions = "\n".join(report["next_actions"])

    assert primary["bot_id"] == "volume_profile_mnq"
    assert primary["active"] is False
    assert primary["deactivation_source"] == "kaizen_sidecar"
    assert "bot row is deactivated via kaizen_sidecar" in primary["blockers"]
    assert primary["live_routing_allowed"] is False
    assert "Keep volume_profile_mnq quarantined" in actions
    assert "ELITE/ROBUST" in actions


def test_ladder_allows_primary_only_when_every_live_gate_passes() -> None:
    report = ladder.build_ladder_report(
        readiness_rows=[
            _row("volume_profile_mnq", "MNQ1", can_live_trade=True),
            _row("volume_profile_nq", "NQ1", can_live_trade=True),
        ],
        strict_gate_metrics={"volume_profile_mnq": {"trades": 2916, "sh_def": 2.86, "L": True, "S": True}},
        prop_readiness={"summary": "READY_FOR_DRY_RUN"},
    )

    primary, runner = report["candidates"][:2]
    assert primary["live_routing_allowed"] is True
    assert primary["role"] == "primary"
    assert runner["live_routing_allowed"] is False
    assert "runner slot is paper/research only" in runner["blockers"]
    assert report["summary"]["automation_mode"] == "PRIMARY_READY_FOR_CONTROLLED_PROP_DRY_RUN"


def test_latest_strict_gate_metrics_merges_newest_evidence_per_bot(tmp_path) -> None:
    older = tmp_path / "strict_gate_older.json"
    newer = tmp_path / "strict_gate_newer.json"
    older.write_text(
        '[{"bot": "volume_profile_mnq", "trades": 2916, "sh_def": 2.86, "L": true, "S": true}]',
        encoding="utf-8",
    )
    newer.write_text(
        '[{"bot": "mym_sweep_reclaim", "trades": 11, "sh_def": -0.16, "L": true, "S": false}]',
        encoding="utf-8",
    )

    metrics = ladder._latest_strict_gate_metrics(tmp_path)

    assert metrics["volume_profile_mnq"]["trades"] == 2916
    assert metrics["mym_sweep_reclaim"]["trades"] == 11


def test_cli_rejects_output_path_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "futures_prop_ladder_latest.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        ladder,
        "_readiness_rows_from_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("readiness should not load")),
    )

    with pytest.raises(SystemExit) as exc:
        ladder.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2

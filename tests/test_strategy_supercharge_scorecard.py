from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts import workspace_roots
from eta_engine.scripts.bot_strategy_readiness import ReadinessRow


def _row(
    bot_id: str,
    *,
    launch_lane: str,
    can_paper_trade: bool,
    promotion_status: str = "research_candidate",
    strategy_id: str | None = None,
    strategy_kind: str = "crypto_orb",
) -> ReadinessRow:
    return ReadinessRow(
        bot_id=bot_id,
        strategy_id=strategy_id or f"{bot_id}_v1",
        strategy_kind=strategy_kind,
        symbol="BTC",
        timeframe="1h",
        active=launch_lane != "deactivated",
        promotion_status=promotion_status,
        baseline_status="baseline_present",
        data_status="ready" if launch_lane != "blocked_data" else "blocked",
        launch_lane=launch_lane,
        can_paper_trade=can_paper_trade,
        can_live_trade=False,
        missing_critical=(),
        missing_optional=(),
        next_action=f"next action for {bot_id}",
    )


def test_scorecard_orders_a_c_targets_before_live_preflight() -> None:
    from eta_engine.scripts.strategy_supercharge_scorecard import build_scorecard

    scorecard = build_scorecard(
        rows=[
            _row("nq_futures", launch_lane="live_preflight", can_paper_trade=True, promotion_status="production"),
            _row("eth_compression", launch_lane="paper_soak", can_paper_trade=True),
            _row("mnq_sage_consensus", launch_lane="research", can_paper_trade=False),
            _row("mnq_futures", launch_lane="shadow_only", can_paper_trade=False),
            _row("crypto_seed", launch_lane="non_edge", can_paper_trade=False),
            _row("xrp_perp", launch_lane="deactivated", can_paper_trade=False),
        ],
        generated_at="2026-04-30T02:05:00+00:00",
    )

    assert scorecard["source"] == "strategy_supercharge_scorecard"
    assert scorecard["strategy"] == "A_C_THEN_B"
    assert scorecard["summary"]["total_bots"] == 6
    assert scorecard["summary"]["a_c_targets"] == 3
    assert scorecard["summary"]["b_later_targets"] == 1
    assert scorecard["summary"]["hold_targets"] == 2
    assert scorecard["summary"]["next_best_bot"] == "eth_compression"

    ordered = [row["bot_id"] for row in scorecard["rows"]]
    assert ordered[:4] == ["eth_compression", "mnq_sage_consensus", "mnq_futures", "nq_futures"]
    assert ordered[-2:] == ["crypto_seed", "xrp_perp"]

    eth = scorecard["rows_by_bot"]["eth_compression"]
    assert eth["supercharge_phase"] == "A_C_PAPER_SOAK"
    assert eth["next_gate"] == "paper_soak_retest"
    assert eth["target_reason"].startswith("Paper-soak bot")

    live = scorecard["rows_by_bot"]["nq_futures"]
    assert live["supercharge_phase"] == "B_LIVE_PREFLIGHT_LATER"
    assert live["next_gate"] == "live_preflight_regression_guard"


def test_scorecard_write_snapshot_round_trips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_scorecard import build_scorecard, write_scorecard

    out = tmp_path / "strategy_supercharge_scorecard_latest.json"
    scorecard = build_scorecard(
        rows=[_row("btc_sage_daily_etf", launch_lane="paper_soak", can_paper_trade=True)],
        generated_at="2026-04-30T02:05:00+00:00",
    )

    written = write_scorecard(scorecard, out)

    assert written == out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["next_best_bot"] == "btc_sage_daily_etf"
    assert payload["rows_by_bot"]["btc_sage_daily_etf"]["supercharge_phase"] == "A_C_PAPER_SOAK"


def test_cli_rejects_output_path_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from eta_engine.scripts import strategy_supercharge_scorecard as scorecard

    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "strategy_supercharge_scorecard_latest.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        scorecard,
        "build_scorecard",
        lambda: (_ for _ in ()).throw(AssertionError("scorecard should not build for rejected output")),
    )

    with pytest.raises(SystemExit) as exc:
        scorecard.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2
    assert not outside_workspace.exists()

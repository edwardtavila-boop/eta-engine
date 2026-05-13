from __future__ import annotations

from eta_engine.scripts import bot_strategy_readiness as mod


def _row(
    bot_id: str,
    symbol: str,
    *,
    promotion_status: str = "production_candidate",
    launch_lane: str = "paper_soak",
) -> mod.ReadinessRow:
    return mod.ReadinessRow(
        bot_id=bot_id,
        strategy_id=f"{bot_id}_v1",
        strategy_kind="confluence_scorecard",
        symbol=symbol,
        timeframe="5m",
        active=True,
        promotion_status=promotion_status,
        baseline_status="baseline_present",
        data_status="ready",
        launch_lane=launch_lane,
        can_paper_trade=True,
        can_live_trade=False,
        missing_critical=(),
        missing_optional=(),
        next_action=f"next action for {bot_id}",
    )


def test_priority_model_puts_futures_and_commodities_before_cellared_spot_crypto() -> None:
    rows = [
        _row("sol_optimized", "SOL"),
        _row("mbt_funding_basis", "MBT1"),
        _row("mcl_sweep_reclaim", "MCL1"),
        _row("volume_profile_nq", "NQ1"),
        _row("eur_sweep_reclaim", "6E1"),
    ]

    prioritized = mod.prioritize_readiness_rows(rows)
    snapshot = mod.build_snapshot(
        prioritized,
        generated_at="2026-05-08T20:00:00+00:00",
        scope="supervisor_pinned",
        supervisor_pinned=tuple(row.bot_id for row in prioritized),
    )

    assert [row.bot_id for row in prioritized] == [
        "volume_profile_nq",
        "mcl_sweep_reclaim",
        "eur_sweep_reclaim",
        "mbt_funding_basis",
        "sol_optimized",
    ]
    assert prioritized[0].priority_bucket == "equity_index_futures"
    assert prioritized[1].priority_bucket == "commodities"
    assert prioritized[-1].priority_bucket == "cellar_spot_crypto"
    assert prioritized[-1].launch_lane == "cellar"
    assert prioritized[-1].can_paper_trade is False
    assert prioritized[0].preferred_broker_stack == (
        "ibkr",
        "tradovate_when_enabled",
        "tastytrade",
    )
    assert prioritized[0].edge_thesis.startswith("Index futures are the funded lead lane")
    assert "volume_profile_value_area" in prioritized[0].primary_edges
    assert "Broker OCO" in prioritized[0].exit_playbook
    assert "MNQ/NQ" in prioritized[0].daily_focus
    assert "event_aware_sweep_reclaim" in prioritized[1].primary_edges
    assert "event-window" in prioritized[1].exit_playbook
    assert "macro-timing" in prioritized[2].edge_thesis
    assert "regulated crypto lane" in prioritized[3].edge_thesis
    assert prioritized[-1].preferred_broker_stack == ("paused_cellar",)
    assert "Alpaca/spot paused" in prioritized[-1].risk_playbook
    assert snapshot["summary"]["priority_focus"] == "futures_and_commodities_first"
    assert snapshot["summary"]["broker_priority"] == [
        "ibkr",
        "tradovate_when_enabled",
        "tastytrade",
    ]
    assert snapshot["summary"]["cellar_buckets"] == ["cellar_spot_crypto"]
    assert snapshot["summary"]["priority_buckets"] == {
        "commodities": 1,
        "cme_crypto_futures": 1,
        "equity_index_futures": 1,
        "rates_fx": 1,
        "cellar_spot_crypto": 1,
    }
    assert snapshot["summary"]["top_priority_bots"] == [
        "volume_profile_nq",
        "mcl_sweep_reclaim",
        "eur_sweep_reclaim",
        "mbt_funding_basis",
        "sol_optimized",
    ]


def test_priority_model_promotes_strict_gate_mnq_leader_within_index_futures() -> None:
    prioritized = mod.prioritize_readiness_rows(
        [
            _row("mnq_futures_sage", "MNQ1"),
            _row("volume_profile_nq", "NQ1"),
            _row("volume_profile_mnq", "MNQ1"),
            _row("rsi_mr_mnq_v2", "MNQ1"),
        ]
    )

    assert [row.bot_id for row in prioritized] == [
        "volume_profile_mnq",
        "volume_profile_nq",
        "mnq_futures_sage",
        "rsi_mr_mnq_v2",
    ]


def test_priority_model_classifies_dated_futures_contract_symbols() -> None:
    prioritized = mod.prioritize_readiness_rows(
        [
            _row("mnq_contract", "MNQM6"),
            _row("met_contract", "METK6"),
            _row("mcl_contract", "MCLM6"),
        ]
    )

    by_bot = {row.bot_id: row for row in prioritized}
    assert by_bot["mnq_contract"].priority_bucket == "equity_index_futures"
    assert by_bot["mnq_contract"].edge_thesis.startswith("Index futures")
    assert by_bot["met_contract"].priority_bucket == "cme_crypto_futures"
    assert by_bot["met_contract"].asset_class == "futures"
    assert by_bot["mcl_contract"].priority_bucket == "commodities"

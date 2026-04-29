from __future__ import annotations

from types import SimpleNamespace

from eta_engine.scripts import paper_live_launch_check as mod


def test_deactivated_bot_is_ready_even_when_data_is_absent(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="xrp_perp",
        strategy_id="xrp_DEACTIVATED",
        strategy_kind="confluence",
        symbol="MNQ1",
        timeframe="1h",
        extras={"deactivated": True},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: False)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: False)
    monkeypatch.setattr(mod, "_check_baseline_persisted", lambda *_: False)

    result = mod._audit_bot(assignment)

    assert result["status"] == "READY"
    assert result["promotion_status"] == "deactivated"
    assert result["issues"] == []
    assert result["warnings"] == []


def test_research_candidate_surfaces_registry_evidence(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mnq_futures",
        strategy_id="mnq_orb_v2",
        strategy_kind="orb",
        symbol="MNQ1",
        timeframe="5m",
        extras={
            "promotion_status": "research_candidate",
            "research_tune": {
                "scope": "latest_20k_bar_research_candidate",
                "source_artifact": "latest.md",
                "candidate_agg_oos_sharpe": 1.788,
                "strict_gate": False,
                "full_history_smoke": {
                    "source_artifact": "full_history.md",
                    "windows": 83,
                    "agg_oos_sharpe": -2.958,
                    "dsr_pass_fraction": 0.132,
                    "strict_gate": False,
                },
            },
        },
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "mnq_orb_v2"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "WARN"
    assert result["warnings"] == [
        "research_candidate (strict gate failed; OOS -2.958; "
        "DSR pass 13.2%; evidence full_history.md)",
    ]
    assert result["evidence"]["baseline_present"] is True
    assert result["evidence"]["scope"] == "latest_20k_bar_research_candidate"
    assert result["evidence"]["full_history_smoke"]["windows"] == 83


def test_research_candidate_without_tune_keeps_generic_warning(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="btc_compression",
        strategy_id="btc_compression_v1",
        strategy_kind="compression_breakout",
        symbol="BTC",
        timeframe="1h",
        extras={"promotion_status": "research_candidate"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(
        mod,
        "_load_baseline_entry",
        lambda *_: {
            "strategy_id": "btc_compression_v1",
            "_walk_forward_summary": "BTC compression strict gate FAIL.",
        },
    )

    result = mod._audit_bot(assignment)

    assert result["status"] == "WARN"
    assert result["warnings"] == ["research_candidate (gate not fully passed)"]
    assert result["evidence"]["baseline_summary"] == "BTC compression strict gate FAIL."

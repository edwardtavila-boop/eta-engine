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
                "candidate_agg_is_sharpe": -0.306,
                "candidate_agg_oos_sharpe": 1.788,
                "candidate_degradation": 0.191,
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
    assert result["evidence"]["candidate_agg_is_sharpe"] == -0.306
    assert result["evidence"]["candidate_degradation"] == 0.191
    assert result["evidence"]["full_history_smoke"]["windows"] == 83


def test_shadow_benchmark_is_ready_with_shadow_evidence(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mnq_futures",
        strategy_id="mnq_orb_v2",
        strategy_kind="orb",
        symbol="MNQ1",
        timeframe="5m",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "plain ORB failed full-history validation",
        },
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "mnq_orb_v2"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "READY"
    assert result["warnings"] == []
    assert result["promotion_status"] == "shadow_benchmark"
    assert result["evidence"]["launch_role"] == "shadow_only"
    assert result["evidence"]["shadow_reason"] == "plain ORB failed full-history validation"


def test_non_edge_strategy_is_ready_with_exposure_evidence(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="crypto_seed",
        strategy_id="crypto_seed_dca",
        strategy_kind="confluence",
        symbol="BTC",
        timeframe="D",
        extras={
            "promotion_status": "non_edge_strategy",
            "non_edge_reason": "DCA exposure accumulator, not an edge strategy",
        },
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "crypto_seed_dca"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "READY"
    assert result["warnings"] == []
    assert result["promotion_status"] == "non_edge_strategy"
    assert result["evidence"]["launch_role"] == "non_edge_exposure"
    assert result["evidence"]["non_edge_reason"] == "DCA exposure accumulator, not an edge strategy"


def test_research_warning_includes_is_and_degradation_when_present() -> None:
    warning = mod._research_warning(
        {
            "research_tune": {
                "strict_gate": False,
                "source_artifact": "sol.md",
                "candidate_agg_is_sharpe": -0.306,
                "candidate_agg_oos_sharpe": 2.489,
                "candidate_dsr_pass_fraction": 0.524,
                "candidate_degradation": 0.191,
            }
        }
    )

    assert warning == (
        "research_candidate (strict gate failed; OOS +2.489; IS -0.306; "
        "DSR pass 52.4%; degradation 19.1%; evidence sol.md)"
    )


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

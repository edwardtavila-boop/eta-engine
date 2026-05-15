from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eta_engine.data.library import DataLibrary
from eta_engine.scripts import paper_live_launch_check as mod

_ORIGINAL_CHECK_CRITICAL_DATA_REQUIREMENTS = mod._check_critical_data_requirements


@pytest.fixture(autouse=True)
def _neutral_data_freshness(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_check_data_freshness", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_check_critical_data_requirements",
        lambda *_args, **_kwargs: {"issues": [], "warnings": [], "evidence": []},
    )
    monkeypatch.setattr(mod, "_latest_runtime_research_report", lambda *_args, **_kwargs: None)


def test_deactivated_bot_is_warn_even_when_data_is_absent(monkeypatch) -> None:
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

    assert result["status"] == "WARN"
    assert result["promotion_status"] == "deactivated"
    assert result["issues"] == []
    assert result["warnings"] == ["deactivated; excluded from launch"]
    assert result["evidence"]["launch_role"] == "deactivated"
    assert result["evidence"]["deactivation_source"] == "registry"


def test_kaizen_sidecar_deactivation_is_warn_not_ready(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="volume_profile_mnq",
        strategy_id="volume_profile_mnq_v1",
        strategy_kind="confluence_scorecard",
        symbol="MNQ1",
        timeframe="5m",
        extras={"promotion_status": "production_candidate"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_check_baseline_persisted", lambda *_: True)
    monkeypatch.setattr(
        "eta_engine.strategies.per_bot_registry.kaizen_deactivation_record",
        lambda bot_id: {"reason": "negative expectancy", "tier": "DECAY"} if bot_id == "volume_profile_mnq" else {},
    )

    result = mod._audit_bot(assignment)

    assert result["status"] == "WARN"
    assert result["promotion_status"] == "deactivated"
    assert result["warnings"] == ["deactivated; excluded from launch"]
    assert result["evidence"]["kaizen_deactivated"] is True
    assert result["evidence"]["deactivation_source"] == "kaizen_sidecar"


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
        "research_candidate (strict gate failed; OOS -2.958; DSR pass 13.2%; evidence full_history.md)",
    ]
    assert result["evidence"]["baseline_present"] is True
    assert result["evidence"]["scope"] == "latest_20k_bar_research_candidate"
    assert result["evidence"]["candidate_agg_is_sharpe"] == -0.306
    assert result["evidence"]["candidate_degradation"] == 0.191
    assert result["evidence"]["full_history_smoke"]["windows"] == 83


def test_research_candidate_prefers_latest_runtime_grid_evidence(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mes_sweep_reclaim_v2",
        strategy_id="mes_sweep_reclaim_v2",
        strategy_kind="sweep_reclaim",
        symbol="MES1",
        timeframe="5m",
        extras={
            "promotion_status": "research_candidate",
            "research_tune": {
                "source_artifact": "old_registry.md",
                "strict_gate": False,
                "candidate_agg_oos_sharpe": -1.0,
                "full_history_smoke": {
                    "source_artifact": "old_full_history.md",
                    "windows": 4,
                    "agg_oos_sharpe": -1.0,
                    "dsr_pass_fraction": 0.0,
                    "strict_gate": False,
                },
            },
        },
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "mes_sweep_reclaim_v2"})
    monkeypatch.setattr(
        mod,
        "_latest_runtime_research_report",
        lambda bot_id: {
            "report_path": "fresh_runtime.md",
            "windows": 11,
            "is_sharpe": -0.059,
            "oos_sharpe": 0.499,
            "dsr_pass_fraction": 0.273,
            "degradation_pct": 27.7,
            "verdict": "FAIL",
            "result_status": "fail",
            "artifact_class": "low_signal",
            "report_mtime": 1778830399.3842053,
        }
        if bot_id == "mes_sweep_reclaim_v2"
        else None,
    )

    result = mod._audit_bot(assignment)

    assert result["status"] == "WARN"
    assert result["warnings"] == [
        "research_candidate (strict gate failed; OOS +0.499; IS -0.059; "
        "DSR pass 27.3%; degradation 27.7%; evidence fresh_runtime.md)",
    ]
    assert result["evidence"]["registry_full_history_source_artifact"] == "old_full_history.md"
    assert result["evidence"]["full_history_smoke"]["source_artifact"] == "fresh_runtime.md"
    assert result["evidence"]["full_history_smoke"]["windows"] == 11
    assert result["evidence"]["runtime_research_grid"]["artifact_class"] == "low_signal"


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


def test_research_candidate_without_registry_tune_uses_runtime_grid(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mes_sweep_reclaim",
        strategy_id="mes_sweep_reclaim_v1",
        strategy_kind="confluence_scorecard",
        symbol="MES1",
        timeframe="1h",
        extras={"promotion_status": "research_candidate"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: None)
    monkeypatch.setattr(
        mod,
        "_latest_runtime_research_report",
        lambda bot_id: {
            "report_path": "fresh_sparse_runtime.md",
            "windows": 26,
            "is_sharpe": 1.895,
            "oos_sharpe": -4.46,
            "dsr_pass_fraction": 0.385,
            "degradation_pct": 55.5,
            "verdict": "FAIL",
            "result_status": "fail",
            "artifact_class": "low_signal",
            "report_mtime": 1778831124.384267,
        }
        if bot_id == "mes_sweep_reclaim"
        else None,
    )

    result = mod._audit_bot(assignment)

    assert result["warnings"] == [
        "research_candidate (strict gate failed; OOS -4.460; IS +1.895; "
        "DSR pass 38.5%; degradation 55.5%; evidence fresh_sparse_runtime.md)",
        "baseline not in strategy_baselines.json",
    ]
    assert result["evidence"]["full_history_smoke"]["source_artifact"] == "fresh_sparse_runtime.md"
    assert result["evidence"]["runtime_research_grid"]["result_status"] == "fail"


def test_stale_launch_data_warns_without_blocking(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mnq_futures_sage",
        strategy_id="mnq_orb_sage_v1",
        strategy_kind="orb_sage_gated",
        symbol="MNQ1",
        timeframe="5m",
        extras={"promotion_status": "promoted"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(
        mod,
        "_check_data_freshness",
        lambda *_args, **_kwargs: {
            "dataset_key": "MNQ1/5m/history",
            "status": "stale",
            "age_days": 15.02,
            "end": "2026-04-14T19:00:00+00:00",
            "rows": 490_103,
        },
    )
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "mnq_orb_sage_v1"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "WARN"
    assert result["issues"] == []
    assert result["warnings"] == ["stale data: MNQ1/5m ended 2026-04-14 (15.02d old)"]
    assert result["evidence"]["data_freshness"]["dataset_key"] == "MNQ1/5m/history"


def test_anchor_sweep_kind_is_resolvable(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="nq_anchor_sweep",
        strategy_id="nq_anchor_sweep_v1",
        strategy_kind="anchor_sweep",
        symbol="NQ1",
        timeframe="5m",
        extras={"promotion_status": "promoted"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "nq_anchor_sweep_v1"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "READY"
    assert not any(str(issue).startswith("unknown strategy_kind") for issue in result["issues"])


def test_bridge_backed_mbt_kinds_are_resolvable(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mbt_rth_orb",
        strategy_id="mbt_rth_orb_v1",
        strategy_kind="mbt_rth_orb",
        symbol="MBT1",
        timeframe="5m",
        extras={"promotion_status": "research_candidate"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "mbt_rth_orb_v1"})

    result = mod._audit_bot(assignment)

    assert not any(str(issue).startswith("unknown strategy_kind") for issue in result["issues"])


def test_default_launch_scope_excludes_research_and_diagnostic_lanes() -> None:
    launchable = SimpleNamespace(extras={"promotion_status": "paper_soak"})
    research = SimpleNamespace(extras={"promotion_status": "research_candidate"})
    shadow = SimpleNamespace(extras={"promotion_status": "shadow_benchmark"})
    deactivated = SimpleNamespace(extras={"promotion_status": "paper_soak", "deactivated": True})

    assert mod._assignment_in_scope(launchable, "launchable") is True
    assert mod._assignment_in_scope(research, "launchable") is False
    assert mod._assignment_in_scope(shadow, "launchable") is False
    assert mod._assignment_in_scope(deactivated, "launchable") is False


def test_supervisor_pinned_scope_includes_research_candidates() -> None:
    research = SimpleNamespace(
        bot_id="mym_sweep_reclaim",
        extras={"promotion_status": "research_candidate"},
    )

    assert (
        mod._assignment_in_scope(
            research,
            "supervisor_pinned",
            frozenset({"mym_sweep_reclaim"}),
        )
        is True
    )
    assert (
        mod._assignment_in_scope(
            research,
            "supervisor_pinned",
            frozenset({"volume_profile_mnq"}),
        )
        is False
    )


def test_supervisor_pinned_bot_parser_reads_runner_pin(tmp_path: Path) -> None:
    runner = tmp_path / "runner.cmd"
    runner.write_text(
        '@echo off\nset "ETA_SUPERVISOR_BOTS=volume_profile_mnq, mym_sweep_reclaim,,mcl_sweep_reclaim"\n',
        encoding="utf-8",
    )

    assert mod._supervisor_pinned_bot_ids(runner) == frozenset(
        {
            "volume_profile_mnq",
            "mym_sweep_reclaim",
            "mcl_sweep_reclaim",
        }
    )


def test_build_payload_summarizes_status_counts() -> None:
    payload = mod._build_payload(
        results=[
            {"bot_id": "ready", "status": "READY"},
            {"bot_id": "warn", "status": "WARN"},
            {"bot_id": "block", "status": "BLOCK"},
        ],
        scope="supervisor_pinned",
        supervisor_pins=frozenset({"ready", "warn", "block"}),
    )

    assert payload["schema_version"] == 1
    assert payload["source"] == "paper_live_launch_check"
    assert payload["scope"] == "supervisor_pinned"
    assert payload["summary"] == {"ready": 1, "warn": 1, "block": 1}
    assert payload["supervisor_pinned"] == ["block", "ready", "warn"]


def test_write_snapshot_creates_canonical_payload(tmp_path: Path) -> None:
    path = tmp_path / "state" / "paper_live_launch_check_latest.json"
    payload = mod._build_payload(
        results=[{"bot_id": "ready", "status": "READY"}],
        scope="launchable",
        supervisor_pins=frozenset(),
    )

    written = mod.write_snapshot(payload, path)

    assert written == path
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == "paper_live_launch_check"


def test_missing_critical_support_feed_blocks(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="nq_futures",
        strategy_id="nq_orb_v1",
        strategy_kind="orb",
        symbol="NQ1",
        timeframe="5m",
        extras={"promotion_status": "promoted"},
    )
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(
        mod,
        "_check_critical_data_requirements",
        lambda *_args, **_kwargs: {
            "issues": ["missing critical feed: correlation:ES1/5m"],
            "warnings": [],
            "evidence": [],
        },
    )
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "nq_orb_v1"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "BLOCK"
    assert result["issues"] == ["missing critical feed: correlation:ES1/5m"]
    assert result["warnings"] == []


def test_stale_critical_support_feed_warns_with_evidence(monkeypatch) -> None:
    assignment = SimpleNamespace(
        bot_id="mnq_futures_sage",
        strategy_id="mnq_orb_sage_v1",
        strategy_kind="orb_sage_gated",
        symbol="MNQ1",
        timeframe="5m",
        extras={"promotion_status": "promoted"},
    )
    support_evidence = [
        {
            "requirement": {
                "kind": "correlation",
                "symbol": "ES1",
                "timeframe": "5m",
                "critical": True,
                "note": "ES correlation is a primary MNQ price driver",
            },
            "dataset_key": "ES1/5m/history",
            "status": "stale",
            "age_days": 15.02,
            "end": "2026-04-14T19:00:00+00:00",
            "rows": 491_074,
        }
    ]
    monkeypatch.setattr(mod, "_check_data_available", lambda *_: True)
    monkeypatch.setattr(
        mod,
        "_check_critical_data_requirements",
        lambda *_args, **_kwargs: {
            "issues": [],
            "warnings": ["stale critical feed: correlation:ES1/5m ended 2026-04-14 (15.02d old)"],
            "evidence": support_evidence,
        },
    )
    monkeypatch.setattr(mod, "_check_bot_dir_exists", lambda *_: True)
    monkeypatch.setattr(mod, "_load_baseline_entry", lambda *_: {"strategy_id": "mnq_orb_sage_v1"})

    result = mod._audit_bot(assignment)

    assert result["status"] == "WARN"
    assert result["issues"] == []
    assert result["warnings"] == [
        "stale critical feed: correlation:ES1/5m ended 2026-04-14 (15.02d old)",
    ]
    assert result["evidence"]["critical_data_requirements"] == support_evidence


def test_critical_requirement_helper_reports_missing_non_primary_feeds(tmp_path: Path) -> None:
    """When only the primary dataset is seeded, the helper must report
    every OTHER critical requirement as missing.

    Pin history:
    - Originally ``nq_futures`` (4 missing feeds) — deactivated DIAMOND CUT 2026-05-02
    - Then ``mnq_futures_sage`` (3 critical reqs) — sidecar deactivated
      2026-05-05 after elite-gate found severe overfit (decay -79%)
    - Now ``mnq_anchor_sweep`` (2 critical reqs: MNQ1/5m + MNQ1/1h) —
      gate-cleared all 5 lights, promoted to paper_soak.  With MNQ1/5m
      as the primary launch dataset, the helper should report 1 missing
      critical feed (MNQ1/1h).  ES1/5m correlation is critical=False
      for this bot so it doesn't surface as an issue.
    """
    history = tmp_path / "history"
    history.mkdir()
    with (history / "MNQ1_5m.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        writer.writerow([1_735_689_600, 100.0, 101.0, 99.0, 100.5, 10_000.0])

    findings = _ORIGINAL_CHECK_CRITICAL_DATA_REQUIREMENTS(
        "mnq_anchor_sweep",
        primary_symbol="MNQ1",
        primary_timeframe="5m",
        library=DataLibrary(roots=[history]),
    )

    assert findings["warnings"] == []
    assert findings["evidence"] == []
    assert findings["issues"] == [
        "missing critical feed: bars:MNQ1/1h",
    ]


def test_critical_requirement_evidence_includes_resolution_metadata(tmp_path: Path) -> None:
    """Evidence entries for non-primary CRITICAL feeds must include
    resolution metadata so the operator can see exactly how the helper
    resolved each dataset.

    Originally also asserted on synthetic-mode resolution for funding
    + onchain feeds, but those were downgraded from critical=True to
    critical=False in data/requirements.py (notes: 'optional for paper'),
    so the helper correctly excludes them from evidence. The synthetic
    code path still exists (``_resolution_payload`` in announce_data_
    library); if a future feed is promoted to critical AND uses
    synthetic resolution, add an assertion for it here.
    """
    history = tmp_path / "history"
    history.mkdir()
    # Seed every CRITICAL volume_profile_mnq feed: bars MNQ1 5m, 1h, D.
    # Reference-bot history: btc_optimized (retired 2026-05-07) ->
    # volume_profile_btc (retired 2026-05-08, round-4) -> volume_profile_mnq
    # (the only strict-gate survivor as of 2026-05-08 audit). The test
    # validates the CRITICAL-feed evidence pipeline, not BTC vs MNQ
    # specifically.
    for filename in ("MNQ1_5m.csv", "MNQ1_1h.csv", "MNQ1_D.csv"):
        with (history / filename).open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "open", "high", "low", "close", "volume"])
            writer.writerow([1_775_000_000, 100.0, 101.0, 99.0, 100.5, 10_000.0])

    findings = _ORIGINAL_CHECK_CRITICAL_DATA_REQUIREMENTS(
        "volume_profile_mnq",
        primary_symbol="MNQ1",
        primary_timeframe="5m",  # primary excluded from evidence
        library=DataLibrary(roots=[history]),
    )

    evidence_by_key = {item["dataset_key"]: item["resolution"] for item in findings["evidence"]}
    # Non-primary critical feeds get evidence + direct-mode resolution.
    assert evidence_by_key["MNQ1/D/history"]["mode"] == "direct"
    assert evidence_by_key["MNQ1/1h/history"]["mode"] == "direct"
    # Primary (MNQ1/5m) is filtered out of evidence by design.
    assert "MNQ1/5m/history" not in evidence_by_key

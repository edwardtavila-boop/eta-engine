from __future__ import annotations

import json
import os


def _manifest(
    bot_ids: list[str],
    *,
    generated_at: str = "2026-04-30T03:00:00+00:00",
    metadata: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    rows = [
        {
            "bot_id": bot_id,
            "action_type": "research_grid_retest",
            "execution_phase": "A_C_NOW",
            "safe_to_mutate_live": False,
            "writes_live_routing": False,
            **((metadata or {}).get(bot_id) or {}),
        }
        for bot_id in bot_ids
    ]
    return {
        "source": "strategy_supercharge_manifest",
        "status": "ready",
        "generated_at": generated_at,
        "summary": {"a_c_now": len(rows)},
        "next_batch": rows,
        "rows": rows,
    }


def _report(
    tmp_path,  # type: ignore[no-untyped-def]
    name: str,
    bot_id: str,
    *,
    windows: int,
    oos_sharpe: float,
    dsr_pass: float,
    verdict: str,
):
    path = tmp_path / name
    path.write_text(
        "\n".join(
            [
                "# Research Grid - 2026-04-30T03:42:47+00:00",
                "",
                "Artifact class: `low_signal`",
                "",
                (
                    "| Config | Sym/TF | Scorer | Thr | Gate | W | +OOS | IS Sh | "
                    "OOS Sh | Deg% | DSR med | DSR pass% | Verdict | Note |"
                ),
                "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
                (
                    f"| {bot_id} | BTC/1h | btc | 0.0 | - | {windows} | 1 | "
                    f"0.231 | {oos_sharpe:.3f} | 50.0 | 0.500 | {dsr_pass:.1f} | "
                    f"{verdict} | 3240/17275 latest bars |"
                ),
                "",
            ],
        ),
        encoding="utf-8",
    )
    return path


def _current_shape_report(  # type: ignore[no-untyped-def]
    tmp_path,
    name: str,
    bot_id: str,
    *,
    windows: int,
    oos_sharpe: float,
    dsr_pass: float,
    verdict: str,
):
    path = tmp_path / name
    path.write_text(
        "\n".join(
            [
                "# Research Grid - 2026-05-07T22:05:27+00:00",
                "",
                "Artifact class: `promotable`",
                "",
                (
                    "| Config | Sym/TF | Scorer | Thr | Gate | WF | DSR N | W | +OOS | "
                    "IS Sh | OOS Sh | Deg% | DSR med | DSR pass% | Verdict | Note |"
                ),
                "|---|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
                (
                    f"| {bot_id} | MNQ1/5m | mnq | 0.0 | - | anchored | 1 | {windows} | 1 | "
                    f"0.740 | {oos_sharpe:.3f} | 0.0 | 1.000 | {dsr_pass:.1f} | "
                    f"{verdict} | 14304 bars / 73d |"
                ),
                "",
            ],
        ),
        encoding="utf-8",
    )
    return path


def test_results_collect_latest_report_per_manifest_bot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    _report(
        tmp_path,
        "research_grid_20260430_033000_old.md",
        "btc_ensemble_2of3",
        windows=0,
        oos_sharpe=0.0,
        dsr_pass=0.0,
        verdict="FAIL",
    )
    latest = _report(
        tmp_path,
        "research_grid_20260430_034000_new.md",
        "btc_ensemble_2of3",
        windows=2,
        oos_sharpe=0.535,
        dsr_pass=50.0,
        verdict="FAIL",
    )
    _report(
        tmp_path,
        "research_grid_20260430_034500_pass.md",
        "eth_perp",
        windows=3,
        oos_sharpe=1.250,
        dsr_pass=75.0,
        verdict="PASS",
    )

    results = build_results(
        manifest=_manifest(["btc_ensemble_2of3", "eth_perp", "mnq_sage_consensus"]),
        report_dir=tmp_path,
        generated_at="2026-04-30T04:00:00+00:00",
    )

    assert results["source"] == "strategy_supercharge_results"
    assert results["summary"]["tested"] == 2
    assert results["summary"]["passed"] == 1
    assert results["summary"]["failed"] == 1
    assert results["summary"]["pending"] == 1
    assert results["summary"]["next_pending_bot"] == "mnq_sage_consensus"
    btc = results["rows_by_bot"]["btc_ensemble_2of3"]
    assert btc["result_status"] == "fail"
    assert btc["windows"] == 2
    assert btc["oos_sharpe"] == 0.535
    assert btc["dsr_pass_fraction"] == 0.5
    assert btc["report_path"] == str(latest)
    assert results["rows_by_bot"]["eth_perp"]["result_status"] == "pass"
    assert results["rows_by_bot"]["mnq_sage_consensus"]["result_status"] == "pending"


def test_results_parse_current_research_grid_table_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    report = _current_shape_report(
        tmp_path,
        "research_grid_20260507_220527_current.md",
        "mnq_anchor_sweep",
        windows=1,
        oos_sharpe=0.979,
        dsr_pass=100.0,
        verdict="PASS",
    )

    results = build_results(
        manifest=_manifest(["mnq_anchor_sweep"]),
        report_dir=tmp_path,
        generated_at="2026-05-07T22:06:00+00:00",
    )

    row = results["rows_by_bot"]["mnq_anchor_sweep"]
    assert results["summary"]["tested"] == 1
    assert results["summary"]["passed"] == 1
    assert row["result_status"] == "pass"
    assert row["windows"] == 1
    assert row["positive_oos_windows"] == 1
    assert row["oos_sharpe"] == 0.979
    assert row["dsr_pass_fraction"] == 1.0
    assert row["note"] == "14304 bars / 73d"
    assert row["report_path"] == str(report)


def test_results_ignore_reports_older_than_manifest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    old = _report(
        tmp_path,
        "research_grid_20260429_034500_old.md",
        "eth_perp",
        windows=3,
        oos_sharpe=1.250,
        dsr_pass=75.0,
        verdict="PASS",
    )
    os.utime(old, (1_775_000_000, 1_775_000_000))

    results = build_results(
        manifest=_manifest(["eth_perp"], generated_at="2026-04-30T04:00:00+00:00"),
        report_dir=tmp_path,
        generated_at="2026-04-30T04:05:00+00:00",
    )

    row = results["rows_by_bot"]["eth_perp"]
    assert row["result_status"] == "pending"
    assert row["stale_report_path"] == str(old)
    assert results["summary"]["pending"] == 1


def test_results_can_load_canonical_manifest_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    manifest_path = tmp_path / "strategy_supercharge_manifest_latest.json"
    manifest_path.write_text(
        json.dumps(_manifest(["eth_perp"], generated_at="2026-04-30T03:00:00+00:00")),
        encoding="utf-8",
    )
    _report(
        tmp_path,
        "research_grid_20260430_034500_pass.md",
        "eth_perp",
        windows=3,
        oos_sharpe=1.250,
        dsr_pass=75.0,
        verdict="PASS",
    )

    results = build_results(
        manifest_path=manifest_path,
        report_dir=tmp_path,
        generated_at="2026-04-30T04:05:00+00:00",
    )

    assert results["summary"]["tested"] == 1
    assert results["rows_by_bot"]["eth_perp"]["result_status"] == "pass"


def test_results_keep_reports_when_manifest_is_built_inline(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts import strategy_supercharge_manifest
    from eta_engine.scripts.strategy_supercharge_results import build_results

    _current_shape_report(
        tmp_path,
        "research_grid_20260507_220527_current.md",
        "mnq_anchor_sweep",
        windows=1,
        oos_sharpe=0.979,
        dsr_pass=100.0,
        verdict="PASS",
    )
    monkeypatch.setattr(
        strategy_supercharge_manifest,
        "build_manifest",
        lambda: _manifest(["mnq_anchor_sweep"], generated_at="2099-01-01T00:00:00+00:00"),
    )

    results = build_results(
        manifest_path=tmp_path / "missing_manifest.json",
        report_dir=tmp_path,
        generated_at="2026-05-07T22:06:00+00:00",
    )

    assert results["summary"]["tested"] == 1
    assert results["rows_by_bot"]["mnq_anchor_sweep"]["result_status"] == "pass"
    assert results["rows_by_bot"]["mnq_anchor_sweep"]["report_path"].endswith(
        "research_grid_20260507_220527_current.md",
    )


def test_results_rank_near_misses_for_next_retune(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    _report(
        tmp_path,
        "research_grid_20260430_034500_sol.md",
        "sol_perp",
        windows=2,
        oos_sharpe=2.405,
        dsr_pass=50.0,
        verdict="FAIL",
    )
    _report(
        tmp_path,
        "research_grid_20260430_034500_eth.md",
        "eth_compression",
        windows=2,
        oos_sharpe=0.750,
        dsr_pass=50.0,
        verdict="FAIL",
    )
    _report(
        tmp_path,
        "research_grid_20260430_034500_btc.md",
        "btc_hybrid_sage",
        windows=2,
        oos_sharpe=-12.638,
        dsr_pass=0.0,
        verdict="FAIL",
    )

    results = build_results(
        manifest=_manifest(["eth_compression", "btc_hybrid_sage", "sol_perp"]),
        report_dir=tmp_path,
        generated_at="2026-04-30T04:05:00+00:00",
    )

    assert results["summary"]["best_near_miss_bot"] == "sol_perp"
    assert [row["bot_id"] for row in results["near_misses"][:2]] == ["sol_perp", "eth_compression"]
    assert all(row["result_status"] == "fail" for row in results["near_misses"])


def test_results_group_scope_by_symbol_and_strategy_style(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    _report(
        tmp_path,
        "research_grid_20260430_034500_sol.md",
        "sol_perp",
        windows=2,
        oos_sharpe=2.405,
        dsr_pass=50.0,
        verdict="FAIL",
    )
    _report(
        tmp_path,
        "research_grid_20260430_034500_mnq.md",
        "mnq_futures",
        windows=3,
        oos_sharpe=-1.355,
        dsr_pass=0.0,
        verdict="FAIL",
    )

    results = build_results(
        manifest=_manifest(
            ["sol_perp", "mnq_futures"],
            metadata={
                "sol_perp": {"symbol": "SOL", "timeframe": "1h", "strategy_kind": "crypto_orb"},
                "mnq_futures": {"symbol": "MNQ1", "timeframe": "5m", "strategy_kind": "orb"},
            },
        ),
        report_dir=tmp_path,
        generated_at="2026-04-30T04:05:00+00:00",
    )

    assert results["scope"]["label"] == "cross_asset_multi_style"
    assert results["scope"]["symbols"] == ["MNQ1", "SOL"]
    assert results["scope"]["strategy_kinds"] == ["crypto_orb", "orb"]
    assert results["groups"]["by_symbol"]["SOL"]["total_targets"] == 1
    assert results["groups"]["by_symbol"]["SOL"]["best_near_miss_bot"] == "sol_perp"
    assert results["groups"]["by_symbol"]["MNQ1"]["failed"] == 1
    assert results["groups"]["by_strategy_kind"]["crypto_orb"]["symbols"] == ["SOL"]
    assert results["groups"]["by_strategy_kind"]["orb"]["symbols"] == ["MNQ1"]


def test_results_emit_ranked_retune_plan_for_cross_asset_failures(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    _report(
        tmp_path,
        "research_grid_20260430_034500_sol.md",
        "sol_perp",
        windows=2,
        oos_sharpe=2.405,
        dsr_pass=50.0,
        verdict="FAIL",
    )
    _report(
        tmp_path,
        "research_grid_20260430_034500_mnq.md",
        "mnq_futures",
        windows=3,
        oos_sharpe=-1.355,
        dsr_pass=0.0,
        verdict="FAIL",
    )

    results = build_results(
        manifest=_manifest(
            ["sol_perp", "mnq_futures"],
            metadata={
                "sol_perp": {"symbol": "SOL", "timeframe": "1h", "strategy_kind": "crypto_orb"},
                "mnq_futures": {"symbol": "MNQ1", "timeframe": "5m", "strategy_kind": "orb"},
            },
        ),
        report_dir=tmp_path,
        generated_at="2026-04-30T04:05:00+00:00",
    )

    sol_plan = results["rows_by_bot"]["sol_perp"]["retune_plan"]
    mnq_plan = results["rows_by_bot"]["mnq_futures"]["retune_plan"]

    assert sol_plan["issue_code"] == "strict_gate_near_miss"
    assert sol_plan["optimizer_command"][2] == "eta_engine.scripts.fleet_strategy_optimizer"
    assert "--only-bot" in sol_plan["optimizer_command"]
    assert "sol_perp" in sol_plan["optimizer_command"]
    assert any("strategy_supercharge_retunes" in part for part in sol_plan["optimizer_command"])
    assert sol_plan["primary_knobs"] == ["range_minutes", "atr_stop_mult", "rr_target"]
    assert sol_plan["priority_score"] > mnq_plan["priority_score"]
    assert results["retune_queue"][0]["bot_id"] == "sol_perp"
    assert results["retune_queue"][0]["symbol"] == "SOL"
    assert results["retune_queue"][0]["strategy_kind"] == "crypto_orb"
    assert results["retune_queue"][0]["issue_code"] == "strict_gate_near_miss"


def test_results_classify_pending_data_repair_rows_separately(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results

    manifest = _manifest(
        ["cl_sweep_reclaim"],
        metadata={
            "cl_sweep_reclaim": {
                "action_type": "data_repair_recheck",
                "next_gate": "data_repair_before_retune",
                "missing_critical": ["bars:CL/5m"],
                "command": [
                    "python",
                    "-m",
                    "eta_engine.scripts.bot_strategy_readiness",
                    "--bot-id",
                    "cl_sweep_reclaim",
                    "--snapshot",
                    "--json",
                    "--no-write",
                ],
            },
        },
    )

    results = build_results(
        manifest=manifest,
        report_dir=tmp_path,
        generated_at="2026-04-30T04:05:00+00:00",
    )

    plan = results["rows_by_bot"]["cl_sweep_reclaim"]["retune_plan"]
    queue_item = results["retune_queue"][0]
    assert plan["issue_code"] == "data_repair_required"
    assert plan["optimizer_command"][2] == "eta_engine.scripts.bot_strategy_readiness"
    assert "data-repair recheck" in plan["next_step"]
    assert queue_item["issue_code"] == "data_repair_required"
    assert "do not retune" in queue_item["next_step"]


def test_results_write_snapshot_round_trips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_results import build_results, write_results

    _report(
        tmp_path,
        "research_grid_20260430_034500_pass.md",
        "eth_perp",
        windows=3,
        oos_sharpe=1.250,
        dsr_pass=75.0,
        verdict="PASS",
    )
    out = tmp_path / "strategy_supercharge_results_latest.json"
    results = build_results(
        manifest=_manifest(["eth_perp"]),
        report_dir=tmp_path,
        generated_at="2026-04-30T04:00:00+00:00",
    )

    written = write_results(results, out)

    assert written == out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["passed"] == 1
    assert payload["rows_by_bot"]["eth_perp"]["result_status"] == "pass"

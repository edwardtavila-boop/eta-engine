from __future__ import annotations

import json
import os


def _manifest(bot_ids: list[str], *, generated_at: str = "2026-04-30T03:00:00+00:00") -> dict[str, object]:
    rows = [
        {
            "bot_id": bot_id,
            "action_type": "research_grid_retest",
            "execution_phase": "A_C_NOW",
            "safe_to_mutate_live": False,
            "writes_live_routing": False,
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

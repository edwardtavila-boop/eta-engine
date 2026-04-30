from __future__ import annotations

import json


def _row(
    bot_id: str,
    *,
    phase: str,
    gate: str,
    rank: int,
    strategy_kind: str = "crypto_orb",
    timeframe: str = "1h",
    window_days: int = 90,
) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "strategy_id": f"{bot_id}_v1",
        "strategy_kind": strategy_kind,
        "symbol": "BTC",
        "timeframe": timeframe,
        "window_days": window_days,
        "supercharge_phase": phase,
        "supercharge_rank": rank,
        "next_gate": gate,
        "safe_to_mutate_live": False,
    }


def test_manifest_builds_a_c_queue_before_deferred_b() -> None:
    from eta_engine.scripts.strategy_supercharge_manifest import build_manifest

    manifest = build_manifest(
        scorecard={
            "source": "strategy_supercharge_scorecard",
            "strategy": "A_C_THEN_B",
            "summary": {"total_bots": 4},
            "rows": [
                _row("btc_live", phase="B_LIVE_PREFLIGHT_LATER", gate="live_preflight_regression_guard", rank=4),
                _row("btc_research", phase="A_C_RESEARCH_RETEST", gate="research_grid_retest", rank=1),
                _row("eth_hold", phase="HOLD_NON_EDGE", gate="no_promotion_gate", rank=8),
                _row("eth_soak", phase="A_C_PAPER_SOAK", gate="paper_soak_retest", rank=0),
            ],
        },
        generated_at="2026-04-30T04:00:00+00:00",
    )

    assert manifest["source"] == "strategy_supercharge_manifest"
    assert manifest["strategy"] == "A_C_THEN_B"
    assert manifest["summary"]["a_c_now"] == 2
    assert manifest["summary"]["b_deferred"] == 1
    assert manifest["summary"]["hold"] == 1
    assert manifest["summary"]["next_bot"] == "eth_soak"
    assert manifest["next_batch"][0]["bot_id"] == "eth_soak"
    assert manifest["next_batch"][0]["command"][2] == "eta_engine.scripts.run_research_grid"
    assert "--report-policy" in manifest["next_batch"][0]["command"]
    assert "runtime" in manifest["next_batch"][0]["command"]
    assert all(row["safe_to_mutate_live"] is False for row in manifest["rows"])
    assert [row["bot_id"] for row in manifest["b_later"]] == ["btc_live"]
    assert [row["bot_id"] for row in manifest["hold"]] == ["eth_hold"]


def test_manifest_can_append_b_after_a_c_when_requested() -> None:
    from eta_engine.scripts.strategy_supercharge_manifest import build_manifest

    manifest = build_manifest(
        scorecard={
            "source": "strategy_supercharge_scorecard",
            "strategy": "A_C_THEN_B",
            "summary": {"total_bots": 2},
            "rows": [
                _row("btc_live", phase="B_LIVE_PREFLIGHT_LATER", gate="live_preflight_regression_guard", rank=4),
                _row("eth_soak", phase="A_C_PAPER_SOAK", gate="paper_soak_retest", rank=0),
            ],
        },
        include_b_later=True,
        generated_at="2026-04-30T04:00:00+00:00",
    )

    assert [row["bot_id"] for row in manifest["next_batch"]] == ["eth_soak", "btc_live"]
    live = manifest["rows_by_bot"]["btc_live"]
    assert live["execution_phase"] == "B_DEFERRED_UNTIL_A_C_STABLE"
    assert live["command"][2] == "eta_engine.scripts.preflight_bot_promotion"


def test_manifest_write_snapshot_round_trips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from eta_engine.scripts.strategy_supercharge_manifest import build_manifest, write_manifest

    out = tmp_path / "strategy_supercharge_manifest_latest.json"
    manifest = build_manifest(
        scorecard={
            "source": "strategy_supercharge_scorecard",
            "strategy": "A_C_THEN_B",
            "summary": {"total_bots": 1},
            "rows": [
                _row("btc_research", phase="A_C_RESEARCH_RETEST", gate="research_grid_retest", rank=1),
            ],
        },
        generated_at="2026-04-30T04:00:00+00:00",
    )

    written = write_manifest(manifest, out)

    assert written == out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["next_bot"] == "btc_research"
    assert payload["rows_by_bot"]["btc_research"]["action_type"] == "research_grid_retest"


def test_manifest_smoke_cap_covers_walk_forward_window() -> None:
    from eta_engine.scripts.strategy_supercharge_manifest import build_manifest

    manifest = build_manifest(
        scorecard={
            "source": "strategy_supercharge_scorecard",
            "strategy": "A_C_THEN_B",
            "summary": {"total_bots": 1},
            "rows": [
                _row(
                    "btc_ensemble_2of3",
                    phase="A_C_PAPER_SOAK",
                    gate="paper_soak_retest",
                    rank=0,
                    timeframe="1h",
                    window_days=90,
                ),
            ],
        },
        generated_at="2026-04-30T04:00:00+00:00",
    )

    smoke = manifest["next_batch"][0]["smoke_command"]
    max_bars = int(smoke[smoke.index("--max-bars-per-cell") + 1])
    assert max_bars >= 3240

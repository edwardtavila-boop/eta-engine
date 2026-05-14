"""Tests for the wave-21 prop-fund launch readiness gate."""

# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path

# ────────────────────────────────────────────────────────────────────
# Individual gate checks
# ────────────────────────────────────────────────────────────────────


def test_R1_PROP_READY_GO_when_three_designated() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    lb = {"prop_ready_bots": ["a", "b", "c"]}
    g = lr._check_R1_prop_ready_designated(lb)
    assert g.status == "GO"


def test_R1_PROP_READY_HOLD_when_two_designated() -> None:
    """2 PROP_READY bots is below 3 but above the launch-viable minimum."""
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    lb = {"prop_ready_bots": ["a", "b"]}
    g = lr._check_R1_prop_ready_designated(lb)
    assert g.status == "HOLD"


def test_R1_PROP_READY_NO_GO_when_none_designated() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    lb = {"prop_ready_bots": []}
    g = lr._check_R1_prop_ready_designated(lb)
    assert g.status == "NO_GO"


def test_R2_DRAWDOWN_GO_when_signal_OK() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    g = lr._check_R2_drawdown({"signal": "OK", "rationale": "OK"})
    assert g.status == "GO"


def test_R2_DRAWDOWN_HOLD_when_signal_WATCH() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    g = lr._check_R2_drawdown({"signal": "WATCH", "rationale": "buffer low"})
    assert g.status == "HOLD"


def test_R2_DRAWDOWN_NO_GO_when_signal_HALT() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    g = lr._check_R2_drawdown({"signal": "HALT", "rationale": "DD breached"})
    assert g.status == "NO_GO"


def test_R2_DRAWDOWN_detail_preserves_guard_checks() -> None:
    """R2 must carry the actual guard math so stale/blocking states are debuggable."""
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    guard = {
        "ts": "2026-05-13T23:00:00+00:00",
        "signal": "HALT",
        "rationale": "HALT - consistency breached",
        "prop_ready_bots": ["m2k_sweep_reclaim"],
        "daily_pnl_usd": 0.0,
        "total_pnl_usd": 1200.0,
        "consistency_ratio": 0.8333,
        "consistency_check": {
            "name": "consistency",
            "status": "HALT",
            "used_usd": 83.33,
            "limit_usd": 30.0,
            "rationale": "BREACHED - best day ratio 83.33% >= 30% limit",
        },
    }
    g = lr._check_R2_drawdown(guard)

    assert g.status == "NO_GO"
    assert g.detail["receipt_ts"] == "2026-05-13T23:00:00+00:00"
    assert g.detail["total_pnl_usd"] == 1200.0
    assert g.detail["consistency_check"]["status"] == "HALT"
    assert "BREACHED" in g.detail["consistency_check"]["rationale"]


def test_R3_FEED_SANITY_NO_GO_when_prop_ready_bot_flagged() -> None:
    """If a PROP_READY bot has feed-sanity FLAGGED, hard NO_GO."""
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    feed = {
        "scorecards": [
            {"bot_id": "m2k_sweep_reclaim", "verdict": "FLAGGED", "flags": ["STUCK_PRICE"]},
            {"bot_id": "other", "verdict": "CLEAN"},
        ],
    }
    g = lr._check_R3_feed_sanity(feed, prop_ready={"m2k_sweep_reclaim"})
    assert g.status == "NO_GO"


def test_R3_FEED_SANITY_GO_when_non_prop_ready_flagged() -> None:
    """Feed sanity flags on a non-PROP_READY bot don't block launch."""
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    feed = {
        "scorecards": [
            {"bot_id": "mbt_sweep_reclaim", "verdict": "FLAGGED", "flags": ["STUCK_PRICE"]},  # NOT in PROP_READY
            {"bot_id": "m2k_sweep_reclaim", "verdict": "CLEAN"},
        ],
    }
    g = lr._check_R3_feed_sanity(feed, prop_ready={"m2k_sweep_reclaim"})
    assert g.status == "GO"


def test_R4_SIZING_NO_GO_when_prop_ready_bot_BREACHED() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    sizing = {
        "statuses": [
            {"bot_id": "m2k_sweep_reclaim", "verdict": "SIZING_BREACHED"},
        ],
    }
    g = lr._check_R4_sizing(sizing, prop_ready={"m2k_sweep_reclaim"})
    assert g.status == "NO_GO"


def test_R5_WATCHDOG_NO_GO_when_prop_ready_bot_CRITICAL() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    wd = {
        "statuses": [
            {"bot_id": "m2k_sweep_reclaim", "classification": "CRITICAL"},
        ],
    }
    g = lr._check_R5_watchdog(wd, prop_ready={"m2k_sweep_reclaim"})
    assert g.status == "NO_GO"


def test_R5_WATCHDOG_HOLD_when_prop_ready_bot_WARN() -> None:
    """WARN is HOLD (not blocking but operator review needed)."""
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    wd = {
        "statuses": [
            {"bot_id": "m2k_sweep_reclaim", "classification": "WARN"},
        ],
    }
    g = lr._check_R5_watchdog(wd, prop_ready={"m2k_sweep_reclaim"})
    assert g.status == "HOLD"


def test_R6_NO_GO_when_allocator_receipt_missing(tmp_path: Path) -> None:
    """Missing allocator receipt means hourly cron never fired."""
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    g = lr._check_R6_allocator_fresh(tmp_path / "missing.json")
    assert g.status == "NO_GO"


def test_R7_HOLD_when_ledger_over_30_min_old(tmp_path: Path) -> None:
    """Ledger > 0.5h old triggers HOLD (15-min cron should keep < 0.5h)."""
    import os
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    ledger = tmp_path / "ledger.json"
    ledger.write_text("{}", encoding="utf-8")
    # Backdate the file by 1 hour
    old_mtime = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    os.utime(ledger, (old_mtime, old_mtime))
    g = lr._check_R7_ledger_fresh(ledger)
    assert g.status == "HOLD"


# ────────────────────────────────────────────────────────────────────
# Aggregator semantics
# ────────────────────────────────────────────────────────────────────


def test_aggregate_NO_GO_when_any_gate_NO_GO() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    gates = [
        lr.GateResult("a", "GO"),
        lr.GateResult("b", "GO"),
        lr.GateResult("c", "NO_GO"),
        lr.GateResult("d", "HOLD"),
    ]
    verdict, summary = lr._aggregate_verdict(gates)
    assert verdict == "NO_GO"
    assert "c" in summary


def test_aggregate_HOLD_when_any_gate_HOLD_and_no_NO_GO() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    gates = [
        lr.GateResult("a", "GO"),
        lr.GateResult("b", "HOLD"),
        lr.GateResult("c", "GO"),
    ]
    verdict, _ = lr._aggregate_verdict(gates)
    assert verdict == "HOLD"


def test_aggregate_GO_when_all_pass() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    gates = [lr.GateResult(f"g{i}", "GO") for i in range(7)]
    verdict, summary = lr._aggregate_verdict(gates)
    assert verdict == "GO"
    assert "safe to cut over" in summary


def test_default_launch_date_matches_operator_july_8_floor() -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    assert lr.DEFAULT_LAUNCH_DATE == "2026-07-08"


def test_R0_calendar_holds_before_live_capital_date() -> None:
    from datetime import date

    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    g = lr._check_R0_live_capital_calendar(
        date(2026, 7, 8),
        today=date(2026, 5, 21),
    )

    assert g.status == "HOLD"
    assert g.detail["paper_live_required"] is True
    assert "2026-07-08" in g.rationale


def test_R0_calendar_go_on_live_capital_date() -> None:
    from datetime import date

    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    g = lr._check_R0_live_capital_calendar(
        date(2026, 7, 8),
        today=date(2026, 7, 8),
    )

    assert g.status == "GO"
    assert g.detail["paper_live_required"] is False


# ────────────────────────────────────────────────────────────────────
# Days-until-launch math
# ────────────────────────────────────────────────────────────────────


def test_run_computes_days_until_launch(tmp_path: Path, monkeypatch: object) -> None:
    """The receipt carries the days-until-launch countdown."""
    from datetime import date, timedelta

    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    monkeypatch.setattr(lr, "OUT_LATEST", tmp_path / "out.json")  # type: ignore[attr-defined]
    # Use a launch date 5 days in the future
    future_launch = (date.today() + timedelta(days=5)).isoformat()
    summary = lr.run(launch_date_str=future_launch)
    assert summary["launch_date"] == future_launch
    # Days until launch — allow ±1 day for date boundary edge cases
    assert summary["days_until_launch"] in (4, 5, 6)


# ────────────────────────────────────────────────────────────────────
# Snapshot file write
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    from eta_engine.scripts import diamond_prop_launch_readiness as lr

    out_path = tmp_path / "out.json"
    monkeypatch.setattr(lr, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    summary = lr.run()
    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "overall_verdict" in on_disk
    assert "gates" in on_disk
    assert on_disk["overall_verdict"] == summary["overall_verdict"]

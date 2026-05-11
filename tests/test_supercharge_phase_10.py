"""Tests for the supercharge Phase 10 additions:

- l2_contract_roll (futures expiry tracking)
- l2_news_blackout (FOMC/NFP windows)
- l2_heartbeat (daemon liveness)
- l2_reconciliation (broker truth vs supervisor belief)
- l2_strategy_fuse (consecutive-loss circuit breaker)
- l2_daily_summary (operator one-line review)
"""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import (
    l2_contract_roll as roll,
)
from eta_engine.scripts import l2_heartbeat as hb
from eta_engine.scripts import l2_news_blackout as blackout
from eta_engine.scripts import l2_reconciliation as recon
from eta_engine.strategies import l2_strategy_fuse as fuse

# ────────────────────────────────────────────────────────────────────
# l2_contract_roll
# ────────────────────────────────────────────────────────────────────


def test_roll_quarterly_expiry_is_third_friday() -> None:
    # March 2026: 3rd Friday is March 20
    d = roll._quarterly_3rd_friday(2026, date(2026, 1, 1))
    assert d == date(2026, 3, 20)


def test_roll_monthly_last_business_day() -> None:
    # May 2026: 31st is Sunday; last business day = Fri May 29
    d = roll._monthly_last_business_day(2026, 5)
    assert d.weekday() < 5  # Mon-Fri
    assert d.month == 5


def test_roll_zone_urgent_2_days_out() -> None:
    """2 days before expiry → URGENT zone, blocked=True."""
    fake_today = date(2026, 3, 18)  # 3rd Friday is 20th, 2 days out
    verdict = roll.assess_roll_zone("MNQ", today=fake_today)
    assert verdict.zone == "URGENT"
    assert verdict.blocked is True


def test_roll_zone_roll_5_days_out() -> None:
    fake_today = date(2026, 3, 15)  # 5 days before
    verdict = roll.assess_roll_zone("MNQ", today=fake_today)
    assert verdict.zone == "ROLL"
    assert verdict.blocked is False


def test_roll_zone_normal_30_days_out() -> None:
    fake_today = date(2026, 2, 18)
    verdict = roll.assess_roll_zone("MNQ", today=fake_today)
    assert verdict.zone == "NORMAL"


def test_roll_unknown_symbol_returns_unknown() -> None:
    verdict = roll.assess_roll_zone("FAKE_NOT_IN_CYCLE")
    assert verdict.zone == "UNKNOWN"


# ────────────────────────────────────────────────────────────────────
# l2_news_blackout
# ────────────────────────────────────────────────────────────────────


def test_blackout_empty_check_returns_clear(tmp_path: Path) -> None:
    result = blackout.is_in_blackout(
        "MNQ", _path=tmp_path / "events.jsonl")
    assert result.in_blackout is False


def test_blackout_add_and_check(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    blackout.add_event(
        blackout.BlackoutWindow(
            start=(now - timedelta(minutes=10)).isoformat(),
            end=(now + timedelta(minutes=20)).isoformat(),
            reason="FOMC", symbols=["MNQ", "NQ"], note="test",
        ),
        _path=path,
    )
    # Within window
    result = blackout.is_in_blackout("MNQ", when=now, _path=path)
    assert result.in_blackout is True
    assert "FOMC" in result.reason


def test_blackout_symbol_filter_excludes_unaffected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    blackout.add_event(
        blackout.BlackoutWindow(
            start=(now - timedelta(minutes=10)).isoformat(),
            end=(now + timedelta(minutes=20)).isoformat(),
            reason="FOMC", symbols=["MNQ"],  # only MNQ
        ),
        _path=path,
    )
    result = blackout.is_in_blackout("GC", when=now, _path=path)
    assert result.in_blackout is False


def test_blackout_wildcard_affects_all_symbols(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    blackout.add_event(
        blackout.BlackoutWindow(
            start=(now - timedelta(minutes=10)).isoformat(),
            end=(now + timedelta(minutes=20)).isoformat(),
            reason="ALL_HALT", symbols=["*"],
        ),
        _path=path,
    )
    for sym in ("MNQ", "GC", "CL", "NQ"):
        result = blackout.is_in_blackout(sym, when=now, _path=path)
        assert result.in_blackout is True


def test_blackout_outside_window_clears(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    blackout.add_event(
        blackout.BlackoutWindow(
            start=(now + timedelta(hours=1)).isoformat(),
            end=(now + timedelta(hours=2)).isoformat(),
            reason="future", symbols=["MNQ"],
        ),
        _path=path,
    )
    # Now is BEFORE the window
    result = blackout.is_in_blackout("MNQ", when=now, _path=path)
    assert result.in_blackout is False


# ────────────────────────────────────────────────────────────────────
# l2_heartbeat
# ────────────────────────────────────────────────────────────────────


def test_heartbeat_missing_log_reports_not_alive(tmp_path: Path) -> None:
    probe = hb.HeartbeatProbe(
        name="test_daemon",
        log_paths=[tmp_path / "missing.jsonl"],
        expected_interval_seconds=60,
    )
    status = hb.check_probe(probe)
    assert status.alive is False


def test_heartbeat_fresh_log_reports_alive(tmp_path: Path) -> None:
    path = tmp_path / "fresh.jsonl"
    path.write_text("data\n", encoding="utf-8")
    probe = hb.HeartbeatProbe(
        name="test_daemon",
        log_paths=[path],
        expected_interval_seconds=60,
    )
    status = hb.check_probe(probe)
    assert status.alive is True
    assert status.last_signal_age_seconds is not None
    assert status.last_signal_age_seconds < 10


def test_heartbeat_picks_youngest_log(tmp_path: Path) -> None:
    """When multiple log paths are provided, picks the youngest."""
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("old data\n", encoding="utf-8")
    # Make old file's mtime older
    import os
    old_time = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(old, (old_time, old_time))
    new.write_text("new data\n", encoding="utf-8")

    probe = hb.HeartbeatProbe(
        name="multi_log",
        log_paths=[old, new],
        expected_interval_seconds=300,
    )
    status = hb.check_probe(probe)
    # New file fresh → alive (regardless of old file being stale)
    assert status.alive is True


# ────────────────────────────────────────────────────────────────────
# l2_reconciliation
# ────────────────────────────────────────────────────────────────────


def test_recon_no_data_in_sync(tmp_path: Path) -> None:
    report = recon.reconcile(
        _supervisor_path=tmp_path / "sup.json",
        _broker_path=tmp_path / "brk.jsonl",
    )
    assert report.n_discrepancies == 0


def test_recon_ghost_position_detected(tmp_path: Path) -> None:
    """Broker has position, supervisor doesn't know."""
    fill_path = tmp_path / "fill.jsonl"
    now = datetime.now(UTC)
    fill_path.write_text(json.dumps({
        "ts": now.isoformat(),
        "signal_id": "MNQ-LONG-test",
        "broker_exec_id": "x1",
        "exit_reason": "ENTRY",
        "side": "LONG",
        "actual_fill_price": 100.0,
        "qty_filled": 1,
        "bot_id": "mnq_book_imbalance_shadow",
        "symbol": "MNQ",
    }) + "\n", encoding="utf-8")
    # Supervisor knows nothing (empty file)
    sup_path = tmp_path / "sup.json"
    sup_path.write_text("[]", encoding="utf-8")
    report = recon.reconcile(
        _supervisor_path=sup_path, _broker_path=fill_path)
    assert report.n_discrepancies >= 1
    assert any(d.verdict == "GHOST_POSITION" for d in report.discrepancies)


def test_recon_phantom_belief_detected(tmp_path: Path) -> None:
    """Supervisor believes position is open, broker has nothing."""
    sup_path = tmp_path / "sup.json"
    sup_path.write_text(json.dumps([{
        "bot_id": "mnq_book_imbalance_shadow",
        "symbol": "MNQ", "side": "LONG", "qty": 1,
    }]), encoding="utf-8")
    fill_path = tmp_path / "fill.jsonl"
    fill_path.write_text("", encoding="utf-8")
    report = recon.reconcile(
        _supervisor_path=sup_path, _broker_path=fill_path)
    assert any(d.verdict == "PHANTOM_BELIEF" for d in report.discrepancies)


# ────────────────────────────────────────────────────────────────────
# l2_strategy_fuse
# ────────────────────────────────────────────────────────────────────


def test_fuse_initial_state_not_blown(tmp_path: Path) -> None:
    result = fuse.check_fuse(
        "book_imbalance_v1", "MNQ",
        _path=tmp_path / "fuses.json",
    )
    assert result["blocked"] is False


def test_fuse_blows_after_threshold_losses(tmp_path: Path) -> None:
    path = tmp_path / "fuses.json"
    for _ in range(5):
        fuse.record_outcome(
            "book_imbalance_v1", "MNQ", won=False,
            fuse_threshold=5, _path=path,
        )
    result = fuse.check_fuse(
        "book_imbalance_v1", "MNQ", _path=path)
    assert result["blocked"] is True
    assert result["reason"] == "strategy_fuse_blown"


def test_fuse_winning_trade_resets_counter(tmp_path: Path) -> None:
    path = tmp_path / "fuses.json"
    # 3 losses, then a win, then more losses
    for _ in range(3):
        fuse.record_outcome("book_imbalance_v1", "MNQ", won=False,
                              fuse_threshold=5, _path=path)
    fuse.record_outcome("book_imbalance_v1", "MNQ", won=True,
                          fuse_threshold=5, _path=path)
    # Counter should be reset; need 5 more to blow
    for _ in range(4):
        fuse.record_outcome("book_imbalance_v1", "MNQ", won=False,
                              fuse_threshold=5, _path=path)
    result = fuse.check_fuse("book_imbalance_v1", "MNQ", _path=path)
    assert result["blocked"] is False  # only 4 consec losses


def test_fuse_auto_resets_after_cooldown(tmp_path: Path) -> None:
    path = tmp_path / "fuses.json"
    for _ in range(5):
        fuse.record_outcome("book_imbalance_v1", "MNQ", won=False,
                              fuse_threshold=5, _path=path)
    # Check far in the future (after cooldown)
    future = datetime.now(UTC) + timedelta(hours=2)
    result = fuse.check_fuse(
        "book_imbalance_v1", "MNQ",
        cooldown_seconds=3600, now=future, _path=path)
    assert result["blocked"] is False
    assert result["reason"] == "cooldown_elapsed"


def test_fuse_manual_reset(tmp_path: Path) -> None:
    path = tmp_path / "fuses.json"
    for _ in range(5):
        fuse.record_outcome("book_imbalance_v1", "MNQ", won=False,
                              fuse_threshold=5, _path=path)
    assert fuse.check_fuse(
        "book_imbalance_v1", "MNQ", _path=path)["blocked"] is True
    cleared = fuse.reset_fuse("book_imbalance_v1", "MNQ", _path=path)
    assert cleared is True
    assert fuse.check_fuse(
        "book_imbalance_v1", "MNQ", _path=path)["blocked"] is False


# ────────────────────────────────────────────────────────────────────
# l2_daily_summary
# ────────────────────────────────────────────────────────────────────


def test_daily_summary_builds_without_data() -> None:
    """build_summary should work even with empty logs."""
    from eta_engine.scripts import l2_daily_summary as ds
    summary = ds.build_summary()
    assert summary.overall_verdict in ("GREEN", "YELLOW", "RED")
    assert summary.n_strategies >= 1  # registry has entries


def test_daily_summary_slack_format_includes_emoji() -> None:
    from eta_engine.scripts import l2_daily_summary as ds
    summary = ds.build_summary()
    s = ds.format_slack(summary)
    assert ":" in s  # contains slack emoji
    assert "L2 Daily Summary" in s

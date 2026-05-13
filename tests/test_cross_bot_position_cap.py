"""Tests for the cross-bot fleet position cap.

Defends against the concrete Apex blast scenario logged 2026-05-07:
two MBT bots each ship qty=3 SHORT past the per-order cap and combine
into 6 MBT short = ~$48k notional on a $50k equity account; one 1.5x
ATR adverse move = ~$600 MTM = 24% of an Apex Tier-A trailing buffer
in seconds. The fleet position cap is the only gate that catches that
combination upstream of broker submission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.safety.cross_bot_position_tracker import (
    DEFAULT_FALLBACK_CAP,
    DEFAULT_ROOT_CAPS,
    STATE_FILENAME,
    CrossBotPositionTracker,
    FleetPositionCapExceeded,
    PropSleeveCapExceeded,
    assert_fleet_position_cap,
    get_cross_bot_position_tracker,
    normalize_root,
    register_cross_bot_position_tracker,
    resolve_fleet_cap,
    resolve_prop_sleeve_cap,
    signed_delta,
)


@pytest.fixture(autouse=True)
def _clear_singleton():
    """Reset the process-wide tracker before/after every test."""
    register_cross_bot_position_tracker(None)
    yield
    register_cross_bot_position_tracker(None)


def test_normalize_root_handles_common_shapes() -> None:
    assert normalize_root("MBT1") == "MBT"
    assert normalize_root("MBT") == "MBT"
    assert normalize_root("BTCUSD") == "BTC"
    assert normalize_root("BTCUSDT") == "BTC"
    assert normalize_root("/MNQ1") == "MNQ"
    assert normalize_root("mbt") == "MBT"
    assert normalize_root("") == ""


def test_signed_delta_signs() -> None:
    assert signed_delta("BUY", 3) == 3.0
    assert signed_delta("LONG", 2.5) == 2.5
    assert signed_delta("SELL", 3) == -3.0
    assert signed_delta("SHORT", 1) == -1.0
    assert signed_delta("BUY", -3) == 3.0


def test_signed_delta_rejects_unknown_side() -> None:
    with pytest.raises(ValueError, match="BUY/SELL"):
        signed_delta("XYZ", 1)


def test_resolve_fleet_cap_default_for_mbt(monkeypatch) -> None:
    monkeypatch.delenv("ETA_FLEET_POSITION_CAP_MBT", raising=False)
    monkeypatch.delenv("ETA_FLEET_POSITION_CAP_DEFAULT", raising=False)
    assert resolve_fleet_cap("MBT") == DEFAULT_ROOT_CAPS["MBT"]
    assert resolve_fleet_cap("MET") == DEFAULT_ROOT_CAPS["MET"]


def test_resolve_fleet_cap_env_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("ETA_FLEET_POSITION_CAP_MBT", "1")
    assert resolve_fleet_cap("MBT") == 1.0


def test_resolve_fleet_cap_default_env_for_unknown_root(monkeypatch) -> None:
    monkeypatch.delenv("ETA_FLEET_POSITION_CAP_ZZZ", raising=False)
    monkeypatch.setenv("ETA_FLEET_POSITION_CAP_DEFAULT", "5")
    assert resolve_fleet_cap("ZZZ") == 5.0


def test_resolve_fleet_cap_falls_back_when_nothing_set(monkeypatch) -> None:
    monkeypatch.delenv("ETA_FLEET_POSITION_CAP_ZZZ", raising=False)
    monkeypatch.delenv("ETA_FLEET_POSITION_CAP_DEFAULT", raising=False)
    assert resolve_fleet_cap("ZZZ") == DEFAULT_FALLBACK_CAP


def test_resolve_prop_sleeve_cap_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ETA_PROP_SLEEVE_CAP_NASDAQ_MNQ_EQUIV", "6")

    assert resolve_prop_sleeve_cap("NASDAQ") == 6.0


def test_nasdaq_sleeve_blocks_mnq_when_nq_already_open() -> None:
    """1 NQ is 10 MNQ-equivalent, so an additional same-side MNQ entry
    must be blocked when the Nasdaq sleeve cap is 10 MNQ-equivalent."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="NQ", side="BUY", qty=1)

    with pytest.raises(PropSleeveCapExceeded) as excinfo:
        tracker.assert_prop_sleeve_cap(
            symbol_root="MNQ",
            side="BUY",
            requested_delta=1,
            sleeve_cap=10,
        )

    err = excinfo.value
    assert err.sleeve == "NASDAQ"
    assert err.root == "MNQ"
    assert err.current_equiv == 10.0
    assert err.requested_equiv == 1.0
    assert err.proposed_equiv == 11.0
    assert err.sleeve_cap == 10.0


def test_nasdaq_sleeve_allows_reducing_opposite_exposure() -> None:
    """An opposite-side MNQ order that reduces existing NQ-equivalent
    exposure is allowed because it lowers net Nasdaq risk."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="NQ", side="BUY", qty=1)

    tracker.assert_prop_sleeve_cap(
        symbol_root="MNQ",
        side="SELL",
        requested_delta=1,
        sleeve_cap=10,
    )


def test_two_bots_short_mbt_second_is_blocked_at_fleet_cap_3() -> None:
    """The headline scenario: bot A shorts 3 MBT, bot B's 3 MBT short
    is rejected upstream of broker submission."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    assert tracker.net_position("MBT") == -3.0
    with pytest.raises(FleetPositionCapExceeded) as excinfo:
        tracker.assert_fleet_position_cap(
            symbol_root="MBT1",
            side="SELL",
            requested_delta=3,
            fleet_cap=3,
        )
    err = excinfo.value
    assert err.root == "MBT"
    assert err.current_net == -3.0
    assert err.requested_delta == -3.0
    assert err.proposed_total == -6.0
    assert err.fleet_cap == 3.0


def test_long_plus_short_nets_out_to_zero_allowed() -> None:
    """Long 1 + Short 1 on same root = 0 net, both should pass."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="BUY", qty=1)
    assert tracker.net_position("MBT") == 1.0
    tracker.assert_fleet_position_cap(
        symbol_root="MBT",
        side="SELL",
        requested_delta=1,
        fleet_cap=3,
    )
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=1)
    assert tracker.net_position("MBT") == 0.0


def test_short_then_long_back_to_neutral_nets_to_zero() -> None:
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.record_entry(symbol_root="MBT", side="BUY", qty=3)
    assert tracker.net_position("MBT") == 0.0


def test_record_exit_decrements_running_net() -> None:
    """An exit ships the OPPOSITE side of the entry; tracker treats it
    as a fresh signed-delta and the algebra cancels out."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.record_exit(symbol_root="MBT", side="BUY", qty=3)
    assert tracker.net_position("MBT") == 0.0


def test_per_root_isolation() -> None:
    """Cap on MBT must not bleed into MET (or vice versa)."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.assert_fleet_position_cap(
        symbol_root="MET",
        side="SELL",
        requested_delta=3,
        fleet_cap=3,
    )


def test_disabled_gate_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("ETA_FLEET_POSITION_CAP_DISABLED", "1")
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=10)
    tracker.assert_fleet_position_cap(
        symbol_root="MBT",
        side="SELL",
        requested_delta=10,
        fleet_cap=3,
    )


def test_env_caps_resolved_per_call(monkeypatch) -> None:
    """An operator can tighten the cap mid-session via env."""
    tracker = CrossBotPositionTracker()
    monkeypatch.setenv("ETA_FLEET_POSITION_CAP_MBT", "5")
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.assert_fleet_position_cap(
        symbol_root="MBT",
        side="SELL",
        requested_delta=1,
    )
    monkeypatch.setenv("ETA_FLEET_POSITION_CAP_MBT", "3")
    with pytest.raises(FleetPositionCapExceeded):
        tracker.assert_fleet_position_cap(
            symbol_root="MBT",
            side="SELL",
            requested_delta=1,
        )


def test_restart_loads_from_disk(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    state_path.write_text(json.dumps({"MBT": -3.0, "MET": 1.0}), encoding="utf-8")
    tracker = CrossBotPositionTracker(state_path=state_path)
    n = tracker.load()
    assert n == 2
    assert tracker.net_position("MBT") == -3.0
    assert tracker.net_position("MET") == 1.0
    with pytest.raises(FleetPositionCapExceeded):
        tracker.assert_fleet_position_cap(
            symbol_root="MBT",
            side="SELL",
            requested_delta=3,
            fleet_cap=3,
        )


def test_load_missing_file_is_zero_state(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    tracker = CrossBotPositionTracker(state_path=state_path)
    assert tracker.load() == 0
    assert tracker.net_position("MBT") == 0.0


def test_load_corrupt_file_starts_empty(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    state_path.write_text("not valid json {", encoding="utf-8")
    tracker = CrossBotPositionTracker(state_path=state_path)
    assert tracker.load() == 0


def test_load_non_dict_payload_starts_empty(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    state_path.write_text("[1,2,3]", encoding="utf-8")
    tracker = CrossBotPositionTracker(state_path=state_path)
    assert tracker.load() == 0


def test_record_entry_persists_to_disk(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    tracker = CrossBotPositionTracker(state_path=state_path)
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    assert state_path.exists()
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk == {"MBT": -3.0}


def test_record_exit_persists_to_disk(tmp_path: Path) -> None:
    state_path = tmp_path / STATE_FILENAME
    tracker = CrossBotPositionTracker(state_path=state_path)
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.record_exit(symbol_root="MBT", side="BUY", qty=3)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk == {"MBT": 0.0}


def test_reconcile_fixes_drift_broker_truth_wins() -> None:
    """Tracker thinks 0; broker shows 2 MBT short. After resync the
    tracker matches broker truth and the next entry sees -2 in the
    book."""
    tracker = CrossBotPositionTracker()
    assert tracker.net_position("MBT") == 0.0
    tracker.resync_from_broker(by_root={"MBT": -2.0})
    assert tracker.net_position("MBT") == -2.0
    with pytest.raises(FleetPositionCapExceeded):
        tracker.assert_fleet_position_cap(
            symbol_root="MBT",
            side="SELL",
            requested_delta=3,
            fleet_cap=3,
        )
    tracker.assert_fleet_position_cap(
        symbol_root="MBT",
        side="SELL",
        requested_delta=1,
        fleet_cap=3,
    )


def test_reconcile_to_zero_clears_drift() -> None:
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.resync_from_broker(by_root={"MBT": 0.0})
    assert tracker.net_position("MBT") == 0.0
    tracker.assert_fleet_position_cap(
        symbol_root="MBT",
        side="SELL",
        requested_delta=3,
        fleet_cap=3,
    )


def test_reconcile_leaves_unrelated_roots_alone() -> None:
    """An IBKR-only reconcile must NOT zero an Alpaca-held BTC."""
    tracker = CrossBotPositionTracker()
    tracker.record_entry(symbol_root="BTC", side="BUY", qty=0.5)
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    tracker.resync_from_broker(by_root={"MBT": -3.0})
    assert tracker.net_position("BTC") == 0.5
    assert tracker.net_position("MBT") == -3.0


def test_module_assert_noop_when_unregistered() -> None:
    register_cross_bot_position_tracker(None)
    assert_fleet_position_cap(
        symbol_root="MBT",
        side="SELL",
        requested_delta=1000,
        fleet_cap=1,
    )


def test_module_assert_dispatches_to_singleton() -> None:
    tracker = CrossBotPositionTracker()
    register_cross_bot_position_tracker(tracker)
    tracker.record_entry(symbol_root="MBT", side="SELL", qty=3)
    with pytest.raises(FleetPositionCapExceeded):
        assert_fleet_position_cap(
            symbol_root="MBT",
            side="SELL",
            requested_delta=3,
            fleet_cap=3,
        )
    assert get_cross_bot_position_tracker() is tracker


def test_supervisor_init_registers_tracker(tmp_path: Path, monkeypatch) -> None:
    """The supervisor's __init__ must construct and register the tracker
    with a state_path under cfg.state_dir, so a restart can find the
    persisted file."""
    monkeypatch.setenv("ETA_SUPERVISOR_STATE_DIR", str(tmp_path))
    register_cross_bot_position_tracker(None)
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path
    sup = JarvisStrategySupervisor(cfg=cfg)
    tracker = get_cross_bot_position_tracker()
    assert tracker is sup._cross_bot_tracker  # noqa: SLF001
    assert tracker is not None
    assert tracker.state_path == tmp_path / STATE_FILENAME


def test_supervisor_blocks_when_fleet_cap_breached(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Pre-populate a -3 MBT short on the supervisor's tracker; confirm
    the gate raises FleetPositionCapExceeded for any further -3 SHORT
    request and the upstream submit_entry would never be reached."""
    monkeypatch.setenv("ETA_SUPERVISOR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ETA_FLEET_POSITION_CAP_MBT", "3")
    register_cross_bot_position_tracker(None)
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._cross_bot_tracker.record_entry(  # noqa: SLF001
        symbol_root="MBT",
        side="SELL",
        qty=3,
    )
    with pytest.raises(FleetPositionCapExceeded):
        sup._cross_bot_tracker.assert_fleet_position_cap(  # noqa: SLF001
            symbol_root="MBT",
            side="SELL",
            requested_delta=3,
            fleet_cap=3,
        )


def test_supervisor_blocks_nasdaq_sleeve_breach(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class _Verdict:
        final_size_multiplier = 1.0
        consolidated = type("_Consolidated", (), {"final_verdict": "APPROVED"})()

        @staticmethod
        def is_blocked() -> bool:
            return False

    monkeypatch.setenv("ETA_SUPERVISOR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ETA_PROP_SLEEVE_CAP_NASDAQ_MNQ_EQUIV", "10")
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", tmp_path / "lifecycle.json")
    ca.set_bot_lifecycle("mnq_prop_candidate", ca.LIFECYCLE_EVAL_LIVE)
    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (False, "clear"),
    )
    register_cross_bot_position_tracker(None)

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.cfg.data_feed = "unit"
    sup._cross_bot_tracker.record_entry(  # noqa: SLF001
        symbol_root="NQ",
        side="BUY",
        qty=1,
    )
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: None)
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: _Verdict())

    def fail_if_router_reached(**_kwargs):
        raise AssertionError("prop sleeve cap should block before submit_entry")

    monkeypatch.setattr(sup._router, "submit_entry", fail_if_router_reached)  # noqa: SLF001
    bot = BotInstance(
        bot_id="mnq_prop_candidate",
        symbol="MNQ1",
        strategy_kind="confluence_scorecard",
        direction="long",
        cash=5000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-08T20:30:00+00:00",
            "open": 29000.0,
            "high": 29010.0,
            "low": 28990.0,
            "close": 29000.0,
            "volume": 10,
        },
    )

    assert bot.open_position is None
    assert bot.last_aggregation_reject_reason.startswith("prop_sleeve_cap:NASDAQ")
    assert bot.last_aggregation_reject_at

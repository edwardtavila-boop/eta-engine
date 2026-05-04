"""Polish-pass regression tests for the supervisor:
  1. ExecutionRouter._fleet_open_notional_for_symbol aggregates by class.
  2. JarvisStrategySupervisor.reconcile_with_broker surfaces divergence.
  3. v26 _broker_router_rejects_for_bot honors the ETA_V26_REJECT_WINDOW_S.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

# ─── ExecutionRouter fleet aggregation ──────────────────────────


def test_fleet_open_notional_sums_same_class_only(tmp_path) -> None:
    """A BTC bot opens crypto $1k; an MNQ bot opens futures $50k. The
    fleet aggregator queried with symbol='BTC' must return $1k, NOT
    sum across classes."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    bot_btc = BotInstance(
        bot_id="btc1", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot_btc.open_position = {"side": "BUY", "qty": 0.01, "entry_price": 100000.0,
                             "entry_ts": "x", "signal_id": "x"}
    bot_mnq = BotInstance(
        bot_id="mnq1", symbol="MNQ1", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot_mnq.open_position = {"side": "BUY", "qty": 1.0, "entry_price": 27500.0,
                             "entry_ts": "x", "signal_id": "x"}
    bot_eth = BotInstance(
        bot_id="eth1", symbol="ETH", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot_eth.open_position = {"side": "BUY", "qty": 0.5, "entry_price": 2400.0,
                             "entry_ts": "x", "signal_id": "x"}

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    router = ExecutionRouter(
        cfg=cfg, bf_dir=tmp_path,
        bots_ref=lambda: [bot_btc, bot_mnq, bot_eth],
    )

    # Asking about BTC should sum BTC + ETH (both crypto), NOT MNQ
    crypto_total = router._fleet_open_notional_for_symbol("BTC")
    assert abs(crypto_total - (0.01 * 100000.0 + 0.5 * 2400.0)) < 1e-3

    # Asking about MNQ should return ONLY MNQ
    futures_total = router._fleet_open_notional_for_symbol("MNQ1")
    assert abs(futures_total - (1.0 * 27500.0)) < 1e-3


def test_fleet_open_notional_handles_no_positions() -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        ExecutionRouter,
        SupervisorConfig,
    )
    bot = MagicMock()
    bot.symbol = "BTC"
    bot.open_position = None
    router = ExecutionRouter(
        cfg=SupervisorConfig(),
        bf_dir=MagicMock(),
        bots_ref=lambda: [bot],
    )
    assert router._fleet_open_notional_for_symbol("BTC") == 0.0


# ─── reconcile_with_broker ───────────────────────────────────────


def test_reconcile_skips_when_not_paper_live(tmp_path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    findings = sup.reconcile_with_broker()
    assert "skipped_reason" in findings
    assert "paper_sim" in findings["skipped_reason"]


def test_reconcile_detects_broker_only_position(tmp_path) -> None:
    """Broker has 2 MNQ open; supervisor has nothing → broker_only."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.bots = []  # supervisor sees nothing

    fake_venue = MagicMock()
    fake_venue.get_positions = MagicMock()
    with patch(
        "eta_engine.scripts.jarvis_strategy_supervisor._get_live_ibkr_venue",
        return_value=fake_venue,
    ), patch(
        "eta_engine.scripts.jarvis_strategy_supervisor._run_on_live_ibkr_loop",
        return_value=[
            {"symbol": "MNQ", "position": 2.0, "avgCost": 27500.0},
        ],
    ):
        findings = sup.reconcile_with_broker()

    assert findings["broker_only"] == [{"symbol": "MNQ", "broker_qty": 2.0}]
    assert not findings["divergent"]
    assert not findings["supervisor_only"]


def test_reconcile_detects_supervisor_only_position(tmp_path) -> None:
    """Supervisor thinks it has BTC long; broker has nothing → supervisor_only."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="btc_a", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {"side": "BUY", "qty": 0.01, "entry_price": 80000.0,
                         "entry_ts": "x", "signal_id": "x"}
    sup.bots = [bot]

    with patch(
        "eta_engine.scripts.jarvis_strategy_supervisor._get_live_ibkr_venue",
        return_value=MagicMock(),
    ), patch(
        "eta_engine.scripts.jarvis_strategy_supervisor._run_on_live_ibkr_loop",
        return_value=[],  # broker has nothing
    ):
        findings = sup.reconcile_with_broker()

    assert findings["supervisor_only"] == [{"symbol": "BTC", "supervisor_qty": 0.01}]
    assert not findings["broker_only"]


def test_reconcile_match_when_aligned(tmp_path) -> None:
    """Broker and supervisor agree on exposure → matched, no warnings."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_a", symbol="MNQ1", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {"side": "BUY", "qty": 1.0, "entry_price": 27500.0,
                         "entry_ts": "x", "signal_id": "x"}
    sup.bots = [bot]

    with patch(
        "eta_engine.scripts.jarvis_strategy_supervisor._get_live_ibkr_venue",
        return_value=MagicMock(),
    ), patch(
        "eta_engine.scripts.jarvis_strategy_supervisor._run_on_live_ibkr_loop",
        return_value=[{"symbol": "MNQ", "position": 1.0, "avgCost": 27500.0}],
    ):
        findings = sup.reconcile_with_broker()

    assert findings["matched"] == 1
    assert not findings["broker_only"]
    assert not findings["supervisor_only"]
    assert not findings["divergent"]


# ─── v26 freshness window ────────────────────────────────────────


def test_v26_ignores_stale_rejects(tmp_path) -> None:
    """A reject from 1 hour ago must NOT count toward the v26 trigger
    when ETA_V26_REJECT_WINDOW_S=600 (10 min)."""
    from eta_engine.brain.jarvis_v3.policies import v26_fill_confirmation as v26

    fresh_ts = datetime.now(UTC).isoformat()
    stale_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    fake_fills = [
        {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": stale_ts},
        {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": stale_ts},
        {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": stale_ts},
        {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": stale_ts},
    ]
    os.environ["ETA_V26_REJECT_WINDOW_S"] = "600"
    try:
        with patch.object(
            v26, "_load_broker_router_fills_cached", return_value=fake_fills,
        ):
            count = v26._broker_router_rejects_for_bot("vwap_mr_mnq")
            assert count == 0  # all stale, ignored

        # Now mix in 3 fresh ones
        fake_fills.extend([
            {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": fresh_ts},
            {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": fresh_ts},
            {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": fresh_ts},
        ])
        with patch.object(
            v26, "_load_broker_router_fills_cached", return_value=fake_fills,
        ):
            count = v26._broker_router_rejects_for_bot("vwap_mr_mnq")
            assert count == 3  # only the fresh ones counted
    finally:
        os.environ.pop("ETA_V26_REJECT_WINDOW_S", None)


def test_v26_unparseable_timestamp_is_treated_as_stale() -> None:
    """A row with a missing or malformed ts must NOT count as recent."""
    from eta_engine.brain.jarvis_v3.policies import v26_fill_confirmation as v26
    fake_fills = [
        {"bot_id": "vwap_mr_mnq", "status": "rejected"},  # no ts
        {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": "not-a-date"},
        {"bot_id": "vwap_mr_mnq", "status": "rejected", "ts": ""},
    ]
    with patch.object(
        v26, "_load_broker_router_fills_cached", return_value=fake_fills,
    ):
        assert v26._broker_router_rejects_for_bot("vwap_mr_mnq") == 0

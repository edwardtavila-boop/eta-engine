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


# ─── _maybe_exit defers to broker bracket ───────────────────────


def test_maybe_exit_defers_when_broker_bracket_active(tmp_path) -> None:
    """In paper_live with a broker-side bracket attached, _maybe_exit
    must NOT fire for normal P&L moves — broker is authoritative.
    Otherwise we'd double-close (supervisor SELL + broker stop fill)."""
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
        bot_id="btc1", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {
        "side": "BUY", "qty": 0.01, "entry_price": 100.0,
        "entry_ts": "x", "signal_id": "x",
        "broker_bracket": True,         # ← bracket is at the broker
        "bracket_stop": 98.0,           # 2% below entry
        "bracket_target": 103.0,
    }
    sup.bots = [bot]

    # Move price to -2% (would normally trigger supervisor stop) but
    # broker bracket is active → no supervisor exit, just defer.
    sup._router.submit_exit = MagicMock()  # type: ignore[method-assign]
    sup._maybe_exit(bot, {"close": 98.0})
    sup._router.submit_exit.assert_not_called()


def test_maybe_exit_emergency_override_when_2x_stop_breached(tmp_path) -> None:
    """If price moves beyond 2x the bracket stop distance, the broker
    bracket is presumed detached and the supervisor closes manually."""
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
        bot_id="btc1", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {
        "side": "BUY", "qty": 0.01, "entry_price": 100.0,
        "entry_ts": "x", "signal_id": "x",
        "broker_bracket": True,
        "bracket_stop": 98.0,           # 2% — emergency threshold = 4%
        "bracket_target": 103.0,
    }
    sup.bots = [bot]

    fake_rec = MagicMock(side="SELL", qty=0.01, fill_price=94.0, realized_r=-2.0)
    sup._router.submit_exit = MagicMock(return_value=fake_rec)  # type: ignore[method-assign]
    sup._propagate_close = MagicMock()  # type: ignore[method-assign]
    # Price at $94 = -6% from entry, exceeds 2x stop (4%) → emergency
    sup._maybe_exit(bot, {"close": 94.0})
    sup._router.submit_exit.assert_called_once()
    sup._propagate_close.assert_called_once()


def test_maybe_exit_runs_normally_without_broker_bracket(tmp_path) -> None:
    """Paper-test crypto (no broker bracket) keeps the original logic."""
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
        bot_id="btc1", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {
        "side": "BUY", "qty": 0.01, "entry_price": 100.0,
        "entry_ts": "x", "signal_id": "x",
        # broker_bracket absent → supervisor-side exits active
    }
    sup.bots = [bot]

    fake_rec = MagicMock(side="SELL", qty=0.01, fill_price=98.0, realized_r=-1.5)
    sup._router.submit_exit = MagicMock(return_value=fake_rec)  # type: ignore[method-assign]
    sup._propagate_close = MagicMock()  # type: ignore[method-assign]
    # -2% breaches the supervisor's -1.5% stop → exit fires
    sup._maybe_exit(bot, {"close": 98.0})
    sup._router.submit_exit.assert_called_once()


# ─── Per-bot bracket overrides ──────────────────────────────────


def test_compute_bracket_honors_per_bot_overrides() -> None:
    """When the supervisor passes per-bot atr_stop_mult / rr_target, they
    override the global env defaults — keeping live and lab geometry
    aligned per-bot for v27 sharpe-drift to be meaningful."""
    from eta_engine.scripts.bracket_sizing import compute_bracket
    bars = [{"high": 105, "low": 95, "close": 100}] * 16  # ATR = 10
    stop, target, _ = compute_bracket(
        side="BUY", entry_price=100.0, bars=bars,
        stop_mult_override=2.5, target_mult_override=3.5,
    )
    # 100 - 2.5*10 = 75; 100 + 3.5*10 = 135
    assert abs(stop - 75.0) < 1e-6
    assert abs(target - 135.0) < 1e-6


def test_lookup_bot_bracket_params_finds_nested_config() -> None:
    """The helper must locate atr_stop_mult / rr_target in any
    extras['*_config'] dict (eth_sage_daily uses crypto_orb_config)."""
    from unittest.mock import MagicMock

    from eta_engine.scripts import bracket_sizing as bs
    fake_assignment = MagicMock(
        bot_id="eth_sage_daily",
        extras={
            "instrument_class": "crypto",
            "crypto_orb_config": {
                "range_minutes": 120,
                "atr_stop_mult": 2.5,
                "rr_target": 3.0,
            },
        },
    )
    with patch.object(bs, "ASSIGNMENTS", [fake_assignment], create=True), \
         patch(
            "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
            [fake_assignment],
         ):
        sm, tm = bs.lookup_bot_bracket_params("eth_sage_daily")
    assert sm == 2.5
    assert tm == 3.0


def test_lookup_bot_bracket_params_returns_none_for_unknown_bot() -> None:
    from eta_engine.scripts.bracket_sizing import lookup_bot_bracket_params
    sm, tm = lookup_bot_bracket_params("not_a_real_bot_id")
    assert sm is None
    assert tm is None


# ─── Feed-health alerts ─────────────────────────────────────────


def test_emit_feed_health_alerts_above_threshold(tmp_path) -> None:
    """When a feed's empty-rate crosses the threshold, a v3 event
    fires. Subsequent ticks at the same level don't re-alert (dedup)."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._feed_health_alerted = set()

    # 3 ok + 7 empty = 70% empty rate, well above 30% threshold
    snapshot = {"yfinance::MNQ": {"ok": 3, "empty": 7}}
    fake_emit = MagicMock()
    with patch(
        "eta_engine.brain.jarvis_v3.policies._v3_events.emit_event", fake_emit,
    ):
        sup._emit_feed_health_alerts(snapshot)
        sup._emit_feed_health_alerts(snapshot)  # second call should dedup

    assert fake_emit.call_count == 1
    kwargs = fake_emit.call_args.kwargs
    assert kwargs["event"] == "feed_degraded"
    assert kwargs["details"]["feed"] == "yfinance"
    assert kwargs["details"]["empty_rate"] == 0.7


def test_emit_feed_health_alerts_below_threshold_no_event(tmp_path) -> None:
    """Healthy feed (low empty rate) emits nothing."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._feed_health_alerted = set()
    snapshot = {"coinbase::BTC": {"ok": 50, "empty": 1}}  # 2% empty
    fake_emit = MagicMock()
    with patch(
        "eta_engine.brain.jarvis_v3.policies._v3_events.emit_event", fake_emit,
    ):
        sup._emit_feed_health_alerts(snapshot)
    fake_emit.assert_not_called()


def test_emit_feed_health_alerts_skips_low_sample_count(tmp_path) -> None:
    """Below min_samples (default 10), ratio is unreliable — defer."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._feed_health_alerted = set()
    # 0 ok + 3 empty = 100% empty BUT only 3 samples
    snapshot = {"yfinance::ZN": {"ok": 0, "empty": 3}}
    fake_emit = MagicMock()
    with patch(
        "eta_engine.brain.jarvis_v3.policies._v3_events.emit_event", fake_emit,
    ):
        sup._emit_feed_health_alerts(snapshot)
    fake_emit.assert_not_called()


def test_feed_health_alert_resets_when_recovers(tmp_path) -> None:
    """A degraded feed that recovers below threshold must clear from
    the dedup set so a future degradation re-alerts."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._feed_health_alerted = {"yfinance::MNQ"}
    snapshot = {"yfinance::MNQ": {"ok": 95, "empty": 5}}  # recovered to 5%
    with patch(
        "eta_engine.brain.jarvis_v3.policies._v3_events.emit_event",
        MagicMock(),
    ):
        sup._emit_feed_health_alerts(snapshot)
    assert "yfinance::MNQ" not in sup._feed_health_alerted


# ─── Paper-mode bracket-based exit (R-magnitude unsquash) ────────


def test_maybe_exit_uses_planned_bracket_in_paper_mode(tmp_path) -> None:
    """When a paper position has a stored bracket_stop / bracket_target,
    _maybe_exit must trigger ONLY when price crosses one of those levels
    — never via the legacy 1-in-15 random close. Without this, paper
    R-magnitudes were ~100x smaller than lab because trades scratched
    out at trivial price moves before the planned bracket could fire.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._router = ExecutionRouter(
        cfg=cfg, bf_dir=tmp_path, bots_ref=lambda: [],
    )

    bot = BotInstance(
        bot_id="t1", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    # Planned: long entry at 60000, stop 59000 (-1.67%), target 63000 (+5%)
    bot.open_position = {
        "side": "BUY", "qty": 0.001, "entry_price": 60000.0,
        "entry_ts": "x", "signal_id": "s1",
        "bracket_stop": 59000.0, "bracket_target": 63000.0,
    }

    # Bar 1: price drifts to 60010 (+0.017%) — below both stop and target.
    # Legacy logic could have random-closed; new logic must hold.
    sup._maybe_exit(bot, {"close": 60010.0})
    assert bot.open_position is not None, (
        "must NOT exit on trivial price drift when bracket levels exist"
    )

    # Bar 2: price hits bracket_target 63000 → must exit cleanly.
    sup._maybe_exit(bot, {"close": 63000.0})
    assert bot.open_position is None, (
        "must exit when price reaches planned bracket_target"
    )


def test_realized_r_uses_bracket_distance_denominator(tmp_path) -> None:
    """When bracket_stop is stored on the position, submit_exit must
    measure realized_r against the planned stop distance × qty (the
    same denominator the lab uses), not against bot.cash * 0.01. That
    makes live R apples-to-apples comparable to lab expectancy_r.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path, bots_ref=lambda: [])

    bot = BotInstance(
        bot_id="t2", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    # Long 0.01 BTC: entry 60000, stop 59000 → planned risk = 1000 * 0.01 = $10.
    # Exit at 60100 → pnl = +$1.00. realized_r = 1.0 / 10.0 = 0.10.
    # If the old denominator (cash*0.01 = $50) had been used, R would be 0.02.
    bot.open_position = {
        "side": "BUY", "qty": 0.01, "entry_price": 60000.0,
        "entry_ts": "x", "signal_id": "s2",
        "bracket_stop": 59000.0, "bracket_target": 63000.0,
    }

    rec = router.submit_exit(bot=bot, bar={"close": 60100.0})
    assert rec is not None
    # 1.5 bps slippage on the SELL side reduces fill from 60100 → ~60091,
    # so realized_r is ~0.0909, not exactly 0.10. Just verify it lands in
    # the bracket-denominator range, not the legacy cash-denominator range.
    assert 0.05 < (rec.realized_r or 0.0) < 0.20, (
        f"realized_r={rec.realized_r} not in bracket-denominator range "
        "(legacy denominator would have produced ~0.02)"
    )


# ─── open_risk_r counting fix (JARVIS REDUCE-tier brake) ─────────


def test_synthetic_ctx_open_risk_r_uses_planned_stop_distance(tmp_path) -> None:
    """The legacy synthetic context set open_risk_r = float(open_count),
    so once the fleet had 4+ bots open the JARVIS REDUCE tier (cap=3R)
    fired on every entry and slammed every verdict to a 0.5x size cap
    even though real R-at-risk was a fraction of 1R.

    With the fix, open_risk_r is computed from each open position's
    planned bracket-stop distance × qty / (1% of bot.cash). 5 bots each
    risking ~$1.67 / $50 ≈ 0.033R should sum to ~0.17R total, not 5.0R.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    # 5 paper crypto bots, each long with a stored bracket
    sup.bots = []
    for i in range(5):
        b = BotInstance(
            bot_id=f"b{i}", symbol="BTC", strategy_kind="x",
            direction="long", cash=5000.0,
        )
        b.open_position = {
            "side": "BUY", "qty": 0.00167, "entry_price": 60000.0,
            "entry_ts": "x", "signal_id": f"s{i}",
            "bracket_stop": 59000.0, "bracket_target": 63000.0,
        }
        sup.bots.append(b)

    ctx = sup._build_synthetic_ctx(sup.bots[0])
    assert ctx is not None, "synthetic context must build"
    # Real R-at-risk: 5 bots × ($1000 stop × 0.00167 qty / $50 R-unit)
    # = 5 × 0.0334 ≈ 0.167R total. Must be well under the 3R cap.
    assert ctx.equity.open_risk_r < 1.0, (
        f"open_risk_r={ctx.equity.open_risk_r} too high; legacy bug "
        "would have set it to 5.0 (the open_count)"
    )


# ─── close_trade carries actual regime, not hardcoded "neutral" ───


def test_propagate_close_uses_live_regime_label(tmp_path, monkeypatch) -> None:
    """_propagate_close was hardcoding regime="neutral" on every close,
    which collapsed every JARVIS analog into one bucket and prevented
    regime-conditional learning. The fix reads the live regime from
    regime_state.json. This test verifies the regime label propagates.
    """
    from unittest.mock import patch

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        FillRecord,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    bot = BotInstance(
        bot_id="rt", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    rec = FillRecord(
        bot_id="rt", signal_id="sig-1", side="SELL", symbol="BTC",
        qty=0.001, fill_price=60000.0, fill_ts="2026-05-04T12:00:00Z",
        realized_r=0.5, realized_pnl=10.0, paper=True, note="",
    )

    with patch.object(
        sup, "_load_live_regime",
        return_value={
            "primary_regime": "trending_up",
            "macro_bias": "risk_on",
        },
    ), patch(
        "eta_engine.brain.jarvis_v3.feedback_loop.close_trade",
    ) as mock_close:
        sup._propagate_close(bot, rec)

    assert mock_close.called, "close_trade must be invoked"
    kwargs = mock_close.call_args.kwargs
    assert kwargs["regime"] == "trending_up", (
        f"regime={kwargs.get('regime')!r} — must reflect live "
        "regime_state.json, not hardcoded 'neutral'"
    )
    assert kwargs["extra"]["macro_bias"] == "risk_on"


# ─── Sage-driven side override (LONG/SHORT per market regime) ─────


def test_maybe_enter_uses_sage_bias_to_pick_side(tmp_path, monkeypatch) -> None:
    """When ETA_SAGE_DRIVEN_SIDE=1 and Sage's composite bias is SHORT
    with conviction >=0.30, the supervisor should flip a long-registered
    bot to SELL — not fight the prevailing read. This is what the
    'supercharge LONG and SHORT for each strategy' directive needs.
    """
    from unittest.mock import MagicMock, patch

    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_SAGE_DRIVEN_SIDE", "1")

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    bot = BotInstance(
        bot_id="t1", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )

    # Mock Sage probe → composite bias SHORT @ conv 0.7
    mock_report = MagicMock()
    mock_report.conviction = 0.7
    mock_report.composite_bias = Bias.SHORT

    # Mock JARVIS verdict → APPROVED (so submit_entry gets called)
    mock_verdict = MagicMock()
    mock_verdict.is_blocked.return_value = False
    mock_verdict.consolidated.final_verdict = "APPROVED"
    mock_verdict.consolidated.is_blocked.return_value = False
    mock_verdict.final_size_multiplier = 1.0

    submit_mock = MagicMock(return_value=None)

    with patch.object(sup, "_consult_sage_for_bot", return_value=mock_report), \
         patch.object(sup, "_consult_jarvis", return_value=mock_verdict), \
         patch.object(sup, "_router") as router:
        router.submit_entry = submit_mock
        # Force the random dice to fire
        with patch("random.random", return_value=0.0):
            sup._maybe_enter(bot, {"close": 60000.0, "ts": "x"})

    assert submit_mock.called, "submit_entry must be called"
    kwargs = submit_mock.call_args.kwargs
    assert kwargs["side"] == "SELL", (
        f"side={kwargs.get('side')!r} — Sage SHORT conv=0.7 should "
        "have flipped the long-registered bot to SELL"
    )

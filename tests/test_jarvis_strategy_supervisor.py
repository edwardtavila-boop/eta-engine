"""Tests for the JARVIS strategy supervisor."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── MockDataFeed ─────────────────────────────────────────────────


def test_mock_feed_returns_well_formed_bar() -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import MockDataFeed
    feed = MockDataFeed(seed=42)
    bar = feed.get_bar("BTC")
    assert "open" in bar
    assert "close" in bar
    assert "high" in bar
    assert "low" in bar
    assert "volume" in bar
    assert "ts" in bar
    assert bar["high"] >= bar["close"]
    assert bar["low"] <= bar["close"]


def test_mock_feed_walks_over_time() -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import MockDataFeed
    feed = MockDataFeed(seed=42)
    closes = [feed.get_bar("ETH")["close"] for _ in range(10)]
    # Random walk should produce variation
    assert len(set(closes)) > 1


def test_mock_feed_handles_unknown_symbol() -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import MockDataFeed
    feed = MockDataFeed(seed=1)
    bar = feed.get_bar("NEWCOIN")
    assert bar["close"] > 0


# ─── ExecutionRouter ─────────────────────────────────────────────


def test_router_submit_entry_paper_sim(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="test", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bar = {"close": 100.0, "high": 101.0, "low": 99.0, "open": 99.5}
    rec = router.submit_entry(
        bot=bot, signal_id="sig1", side="BUY", bar=bar, size_mult=1.0,
    )
    assert rec is not None
    assert rec.side == "BUY"
    assert rec.symbol == "BTC"
    assert rec.qty > 0
    assert rec.fill_price > 99.0
    assert bot.open_position is not None
    assert bot.n_entries == 1


def test_router_submit_exit_computes_pnl(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="test", symbol="ETH", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    # Enter
    enter_bar = {"close": 100.0, "high": 101.0, "low": 99.0, "open": 99.5}
    router.submit_entry(
        bot=bot, signal_id="sig2", side="BUY", bar=enter_bar, size_mult=1.0,
    )
    # Exit at higher price (winning trade)
    exit_bar = {"close": 105.0, "high": 105.5, "low": 104.0, "open": 104.5}
    rec = router.submit_exit(bot=bot, bar=exit_bar)
    assert rec is not None
    assert rec.side == "SELL"
    assert rec.realized_r is not None
    assert rec.realized_r > 0          # winning trade
    assert bot.realized_pnl > 0
    assert bot.open_position is None
    assert bot.n_exits == 1


def test_router_paper_live_writes_pending(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bar = {"close": 100.0}
    router.submit_entry(
        bot=bot, signal_id="s1", side="BUY", bar=bar, size_mult=1.0,
    )
    pending = tmp_path / "paperlive.pending_order.json"
    assert pending.exists()


def test_router_live_blocked_without_env(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "live"
    cfg.live_money_enabled = False
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="livelock", symbol="BTC", strategy_kind="x",
        direction="long",
    )
    rec = router.submit_entry(
        bot=bot, signal_id="s1", side="BUY",
        bar={"close": 100.0}, size_mult=1.0,
    )
    assert rec is None
    assert bot.open_position is None


# ─── BotInstance ─────────────────────────────────────────────────


def test_bot_instance_serializable() -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance
    bot = BotInstance(
        bot_id="x", symbol="MNQ", strategy_kind="orb",
        direction="long", cash=5000.0,
    )
    # Round-trip via to_state
    s = json.dumps(bot.to_state(), default=str)
    assert "x" in s
    assert "MNQ" in s


# ─── SupervisorConfig ────────────────────────────────────────────


def test_config_defaults_safe() -> None:
    import os

    from eta_engine.scripts.jarvis_strategy_supervisor import SupervisorConfig
    # Remove env vars that affect defaults
    for k in ("ETA_SUPERVISOR_BOTS", "ETA_SUPERVISOR_FEED",
              "ETA_SUPERVISOR_MODE", "ETA_LIVE_MONEY"):
        os.environ.pop(k, None)
    cfg = SupervisorConfig()
    assert cfg.mode == "paper_sim"
    assert cfg.data_feed == "mock"
    assert cfg.live_money_enabled is False
    assert cfg.tick_s > 0


# ─── Supervisor (loop integration, fast smoke) ────────────────────


def test_supervisor_loads_bots_and_bootstraps_jarvis(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.tick_s = 0.05
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    n = sup.load_bots()
    assert n >= 0  # may be 0 if registry is empty in test env; just shouldn't crash
    ok = sup.bootstrap_jarvis()
    assert ok is True


def test_supervisor_one_tick_doesnt_crash_when_no_bots(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.bootstrap_jarvis()
    # No bots loaded -- _tick_once should be a no-op
    sup._tick_once(1)
    sup._write_heartbeat(1)
    assert (cfg.state_dir / "heartbeat.json").exists()


def test_supervisor_writes_heartbeat_with_bots(tmp_path: Path) -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.bots.append(BotInstance(
        bot_id="hb-test", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    ))
    sup._write_heartbeat(42)
    hb_file = cfg.state_dir / "heartbeat.json"
    assert hb_file.exists()
    payload = json.loads(hb_file.read_text(encoding="utf-8"))
    assert payload["tick_count"] == 42
    assert payload["n_bots"] == 1
    assert payload["bots"][0]["bot_id"] == "hb-test"


def test_supervisor_heartbeat_embeds_strategy_readiness(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import json

    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    readiness = tmp_path / "bot_strategy_readiness_latest.json"
    readiness.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-04-29T21:00:00+00:00",
                "source": "bot_strategy_readiness",
                "summary": {
                    "blocked_data": 0,
                    "can_live_any": False,
                    "can_paper_trade": 1,
                    "launch_lanes": {"live_preflight": 1},
                },
                "rows": [
                    {
                        "bot_id": "mnq_futures_sage",
                        "strategy_id": "mnq_orb_sage_v1",
                        "launch_lane": "live_preflight",
                        "data_status": "ready",
                        "promotion_status": "production",
                        "can_paper_trade": True,
                        "can_live_trade": False,
                        "next_action": "Run per-bot promotion preflight before live routing.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod.workspace_roots, "ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", readiness)
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.bots.append(
        BotInstance(
            bot_id="mnq_futures_sage",
            symbol="MNQ1",
            strategy_kind="orb_sage",
            direction="long",
            cash=5000.0,
        )
    )

    sup._write_heartbeat(7)

    payload = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    assert payload["bot_strategy_readiness"]["status"] == "ready"
    assert payload["bot_strategy_readiness"]["summary"]["can_paper_trade"] == 1
    bot = payload["bots"][0]
    assert bot["strategy_readiness"]["launch_lane"] == "live_preflight"
    assert bot["strategy_readiness"]["can_paper_trade"] is True
    assert bot["strategy_readiness"]["can_live_trade"] is False
    assert bot["strategy_readiness"]["next_action"].startswith("Run per-bot promotion")


# --- Synthetic JarvisContext ----------------------------------------


def test_supervisor_builds_synthetic_ctx(tmp_path: Path) -> None:
    """Supervisor must produce a usable JarvisContext per consult.

    JarvisAdmin.request_approval requires either an engine attached to
    the admin or an explicit ctx. The supervisor doesn't run a full
    JarvisContextEngine, so it builds a synthetic neutral context per
    call. This test pins that contract.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.bots.append(BotInstance(
        bot_id="ctx-test", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    ))
    ctx = sup._build_synthetic_ctx(sup.bots[0])
    # The shape we actually need to verify: a non-None pydantic
    # JarvisContext that JarvisAdmin can consume.
    assert ctx is not None
    assert ctx.macro is not None
    assert ctx.equity is not None
    assert ctx.equity.account_equity > 0
    assert 0.0 <= ctx.equity.daily_drawdown_pct < 1.0
    assert ctx.regime is not None
    assert ctx.regime.confidence > 0
    assert ctx.journal is not None
    # JarvisAdmin will refuse to accept an engineless build path, but
    # WILL accept a context. Verify by feeding it through the admin.
    from eta_engine.brain.jarvis_admin import (
        ActionType,
        JarvisAdmin,
        SubsystemId,
        make_action_request,
    )
    admin = JarvisAdmin()
    req = make_action_request(
        subsystem=SubsystemId.BOT_MNQ,
        action=ActionType.ORDER_PLACE,
        rationale="ctx smoke", side="buy", qty=1.0, symbol="BTC",
    )
    # Should NOT raise "JarvisAdmin needs either an engine or an
    # explicit ctx" -- the whole point of the synthetic ctx
    resp = admin.request_approval(req, ctx=ctx)
    assert resp is not None
    assert resp.verdict is not None

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


def test_supervisor_persists_recent_verdict_sidecar(tmp_path, monkeypatch) -> None:
    import json
    from types import SimpleNamespace

    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    runtime_state = tmp_path / "runtime_state"
    supervisor_state = tmp_path / "supervisor_state"
    monkeypatch.setattr(mod.workspace_roots, "ETA_RUNTIME_STATE_DIR", runtime_state)

    cfg = mod.SupervisorConfig()
    cfg.state_dir = supervisor_state
    sup = mod.JarvisStrategySupervisor(cfg=cfg)
    bot = mod.BotInstance(bot_id="mnq_futures_sage", symbol="MNQ1", strategy_kind="orb_sage_gated")

    consolidated = SimpleNamespace(
        ts="2026-05-15T12:20:55+00:00",
        request_id="mnq_futures_sage_d7a207fd",
        final_verdict="APPROVED",
        base_reason="trade_ok",
        subsystem="bot.mnq",
        action="ORDER_PLACE",
    )
    verdict = SimpleNamespace(
        consolidated=consolidated,
        sentiment_pressure_status="risk_on",
        sentiment_modulation="tailwind",
        sentiment_pressure_lead_asset="BTC",
        to_dict=lambda: {
            "consolidated": {
                "ts": consolidated.ts,
                "request_id": consolidated.request_id,
                "final_verdict": consolidated.final_verdict,
                "subsystem": consolidated.subsystem,
                "action": consolidated.action,
            },
            "sentiment_pressure_status": "risk_on",
            "sentiment_modulation": "tailwind",
            "sentiment_pressure_lead_asset": "BTC",
        },
    )

    sup._persist_recent_verdict(bot, verdict, signal_id="mnq_futures_sage_d7a207fd")  # noqa: SLF001
    sup._persist_recent_verdict(bot, verdict, signal_id="mnq_futures_sage_d7a207fd")  # noqa: SLF001

    verdict_path = runtime_state / "bots" / "mnq_futures_sage" / "recent_verdicts.json"
    payload = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["bot_id"] == "mnq_futures_sage"
    assert payload[0]["verdict"] == "APPROVED"
    assert payload[0]["sentiment_modulation"] == "tailwind"
    assert payload[0]["sentiment_pressure_lead_asset"] == "BTC"


def test_consult_sage_for_bot_enriches_context_with_live_book_imbalance(tmp_path, monkeypatch) -> None:
    import json
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import pytest

    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    depth_root = tmp_path / "mnq_data"
    depth_dir = depth_root / "depth"
    depth_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": "MNQ1",
        "bids": [
            {"price": 21000.25, "size": 9},
            {"price": 21000.00, "size": 6},
            {"price": 20999.75, "size": 3},
        ],
        "asks": [
            {"price": 21000.50, "size": 3},
            {"price": 21000.75, "size": 2},
            {"price": 21001.00, "size": 1},
        ],
        "spread": 0.25,
        "mid": 21000.375,
    }
    (depth_dir / "MNQ_20260514.jsonl").write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod.workspace_roots, "MNQ_DATA_ROOT", depth_root)

    captured: dict[str, object] = {}

    def _fake_consult(ctx, **_kwargs):
        captured["ctx"] = ctx
        return SimpleNamespace(
            conviction=0.42,
            composite_bias=SimpleNamespace(value="long"),
            alignment_score=1.0,
            per_school={},
        )

    monkeypatch.setattr("eta_engine.brain.jarvis_v3.sage.consult_sage", _fake_consult)

    cfg = mod.SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = mod.JarvisStrategySupervisor(cfg=cfg)

    bot = mod.BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    peer = mod.BotInstance(
        bot_id="nq_peer",
        symbol="NQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    now = datetime.now(UTC)
    for idx in range(20):
        bot.sage_bars.append(
            {
                "ts": (now - timedelta(minutes=20 - idx)).isoformat(),
                "open": 21000.0 + idx,
                "high": 21001.0 + idx,
                "low": 20999.0 + idx,
                "close": 21000.5 + idx,
                "volume": 1000 + idx,
            }
        )
        peer.sage_bars.append(
            {
                "ts": (now - timedelta(minutes=20 - idx)).isoformat(),
                "open": 18000.0 + idx,
                "high": 18001.0 + idx,
                "low": 17999.0 + idx,
                "close": 18000.5 + idx,
                "volume": 900 + idx,
            }
        )
    sup.bots = [bot, peer]

    report = sup._consult_sage_for_bot(
        bot,
        {"close": 21025.0, "high": 21030.0, "low": 21020.0, "open": 21024.0},
        "long",
        21025.0,
    )

    assert report is not None
    ctx = captured["ctx"]
    assert ctx.order_book_imbalance == pytest.approx(0.5)
    assert ctx.peer_returns is not None
    assert "NQ1" in ctx.peer_returns
    assert ctx.account_equity_usd == pytest.approx(50_000.0)


def test_paper_live_symbol_allowlist_accepts_root_and_contract_alias(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ")
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        _paper_live_allowed_symbols,
        _paper_live_symbol_allowed,
    )

    allowed = _paper_live_allowed_symbols()

    assert _paper_live_symbol_allowed("MNQ", allowed) is True
    assert _paper_live_symbol_allowed("MNQ1", allowed) is True
    assert _paper_live_symbol_allowed("NQ1", allowed) is False
    assert _paper_live_symbol_allowed("GC", allowed) is False


def test_paper_live_symbol_allowlist_empty_allows_all(monkeypatch) -> None:
    monkeypatch.delenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", raising=False)
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        _paper_live_allowed_symbols,
        _paper_live_symbol_allowed,
    )

    assert _paper_live_allowed_symbols() is None
    assert _paper_live_symbol_allowed("GC", None) is True


# ─── ExecutionRouter ─────────────────────────────────────────────


def test_micro_dow_front_month_is_classified_as_futures() -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import _classify_symbol

    assert _classify_symbol("MYM1") == "futures"
    assert _classify_symbol("MYM") == "futures"


def test_micro_dow_entry_uses_whole_contract_pending_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.delenv("ETA_SUPERVISOR_LIVE_MONEY", raising=False)
    monkeypatch.delenv("ETA_SUPERVISOR_MODE", raising=False)
    monkeypatch.setenv("ETA_PAPER_FUTURES_FLOOR", "1")

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.broker_router_pending_dir = tmp_path
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="mym_sweep_reclaim",
        symbol="MYM1",
        strategy_kind="confluence_scorecard",
        direction="long",
        cash=50000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="mym_test",
        side="BUY",
        bar={"close": 50000.0, "high": 50050.0, "low": 49950.0, "open": 50000.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert rec.qty == 1.0
    pending = json.loads((tmp_path / "mym_sweep_reclaim.pending_order.json").read_text())
    assert pending["symbol"] == "MYM1"
    assert pending["qty"] == 1.0


def test_broker_router_pending_entry_does_not_create_local_open_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_FUTURES_FLOOR", "1")

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    router = ExecutionRouter(cfg=cfg, bf_dir=cfg.broker_router_pending_dir)
    bot = BotInstance(
        bot_id="mbt_funding_basis",
        symbol="MBT1",
        strategy_kind="mbt_funding_basis",
        direction="long",
        cash=50_000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="mbt_funding_basis_test",
        side="BUY",
        bar={"close": 80650.0, "high": 80700.0, "low": 80600.0, "open": 80630.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert "broker_router_pending_order" in rec.note
    assert (cfg.broker_router_pending_dir / "mbt_funding_basis.pending_order.json").exists()
    assert bot.open_position is None
    assert bot.n_entries == 0
    assert bot.consecutive_broker_rejects == 0
    assert not (cfg.state_dir / "bots" / "mbt_funding_basis" / "open_position.json").exists()


def test_supervisor_adopts_broker_router_filled_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "router" / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    sup.bots.append(bot)
    monkeypatch.setattr(sup._router, "_get_broker_position_qty", lambda _bot: 1.0)  # noqa: SLF001
    persisted: list[list[dict]] = []
    monkeypatch.setattr(mod, "persist_open_positions", lambda rows: persisted.append(rows))
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    order_ts = now - timedelta(seconds=95)
    broker_fill_ts = now - timedelta(seconds=20)

    signal_id = "mnq_futures_sage_adopt"
    fill_dir = cfg.broker_router_pending_dir.parent / "fill_results"
    fill_dir.mkdir(parents=True, exist_ok=True)
    (fill_dir / f"{signal_id}_result.json").write_text(
        json.dumps(
            {
                "signal_id": signal_id,
                "bot_id": bot.bot_id,
                "venue": "ibkr",
                "order_ts": order_ts.isoformat(),
                "broker_fill_ts": broker_fill_ts.isoformat(),
                "result_written_ts": (now - timedelta(seconds=5)).isoformat(),
                "request": {
                    "symbol": "MNQ",
                    "side": "BUY",
                    "qty": 1.0,
                    "price": 29300.25,
                    "client_order_id": signal_id,
                    "stop_price": 29250.25,
                    "target_price": 29400.25,
                    "reduce_only": False,
                },
                "result": {
                    "order_id": "OID-1",
                    "status": "FILLED",
                    "filled_qty": 1.0,
                    "avg_price": 29300.25,
                },
                "ts": now.isoformat(),
            },
        ),
        encoding="utf-8",
    )

    adopted = sup._adopt_broker_router_fill_if_needed(bot, now=now)  # noqa: SLF001

    assert adopted is True
    assert bot.open_position is not None
    assert bot.open_position["signal_id"] == signal_id
    assert bot.open_position["side"] == "BUY"
    assert bot.open_position["qty"] == 1.0
    assert bot.open_position["entry_price"] == 29300.25
    assert bot.open_position["bracket_stop"] == 29250.25
    assert bot.open_position["bracket_target"] == 29400.25
    assert bot.open_position["broker_router_adopted"] is True
    assert bot.open_position["broker_router_fill_age_s"] == 75.0
    assert bot.open_position["entry_fill_age_s"] == 75.0
    assert bot.open_position["entry_fill_latency_source"] == "broker_router_fill_result"
    assert bot.open_position["entry_fill_age_precision"] == "broker_fill_ts"
    assert bot.open_position["broker_fill_ts"] == broker_fill_ts.isoformat()
    assert bot.open_position["broker_router_result_ts"] == (now - timedelta(seconds=5)).isoformat()
    assert bot.open_position["fill_to_adopt_delay_s"] == 20.0
    assert bot.open_position["fill_result_write_delay_s"] == 15.0
    assert (cfg.state_dir / "bots" / bot.bot_id / "open_position.json").exists()
    assert persisted[-1][0]["bot_id"] == bot.bot_id


def test_supervisor_adopts_stale_shadow_pending_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json
    import os
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_SHADOW_PAPER_PENDING_FALLBACK_S", "30")

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "router" / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
        execution_lane="shadow_paper",
    )
    sup.bots.append(bot)

    persisted: list[list[dict]] = []
    tracker_calls: list[dict[str, object]] = []
    monkeypatch.setattr(mod, "persist_open_positions", lambda rows: persisted.append(rows))
    monkeypatch.setattr(
        sup._cross_bot_tracker,
        "record_entry",
        lambda **kwargs: tracker_calls.append(dict(kwargs)),
    )

    pending_path = cfg.broker_router_pending_dir / f"{bot.bot_id}.pending_order.json"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = datetime.now(UTC) - timedelta(minutes=5)
    pending_path.write_text(
        json.dumps(
            {
                "ts": stale_ts.isoformat(),
                "signal_id": "mnq_shadow_pending",
                "side": "BUY",
                "qty": 1.0,
                "symbol": "MNQ1",
                "limit_price": 29300.25,
                "stop_price": 29250.25,
                "target_price": 29400.25,
                "execution_lane": "shadow_paper",
            },
        ),
        encoding="utf-8",
    )
    os.utime(pending_path, (stale_ts.timestamp(), stale_ts.timestamp()))

    adopted = sup._adopt_stale_shadow_pending_entry_if_needed(bot, now=datetime.now(UTC))  # noqa: SLF001

    assert adopted is True
    assert bot.open_position is not None
    assert bot.open_position["signal_id"] == "mnq_shadow_pending"
    assert bot.open_position["side"] == "BUY"
    assert bot.open_position["qty"] == 1.0
    assert bot.open_position["entry_price"] == 29300.25
    assert bot.open_position["bracket_stop"] == 29250.25
    assert bot.open_position["bracket_target"] == 29400.25
    assert bot.open_position["shadow_pending_adopted"] is True
    assert bot.open_position["shadow_pending_age_s"] >= 300.0
    assert bot.open_position["entry_fill_age_s"] >= 300.0
    assert bot.open_position["entry_fill_latency_source"] == "shadow_pending_fallback"
    assert bot.open_position["entry_fill_age_precision"] == "pending_file_ts_to_supervisor_adopt"
    assert bot.open_position["broker_bracket"] is False
    assert bot.n_entries == 1
    assert bot.last_signal_at == stale_ts.isoformat()
    assert tracker_calls == [{"symbol_root": "MNQ", "side": "BUY", "qty": 1.0}]
    assert pending_path.exists() is False
    adopted_dir = cfg.broker_router_pending_dir.parent / "shadow_adopted"
    adopted_paths = list(adopted_dir.glob("mnq_futures_sage_mnq_shadow_pending*.pending_order.json"))
    assert len(adopted_paths) == 1
    assert bot.open_position["shadow_pending_order_path"] == str(adopted_paths[0])
    assert (cfg.state_dir / "bots" / bot.bot_id / "open_position.json").exists()
    assert persisted[-1][0]["bot_id"] == bot.bot_id


def test_supervisor_does_not_adopt_stale_capital_pending_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json
    import os
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_SHADOW_PAPER_PENDING_FALLBACK_S", "30")

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "router" / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
        execution_lane="capital_execution",
    )
    sup.bots.append(bot)

    pending_path = cfg.broker_router_pending_dir / f"{bot.bot_id}.pending_order.json"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = datetime.now(UTC) - timedelta(minutes=5)
    pending_path.write_text(
        json.dumps(
            {
                "ts": stale_ts.isoformat(),
                "signal_id": "mnq_capital_pending",
                "side": "BUY",
                "qty": 1.0,
                "symbol": "MNQ1",
                "limit_price": 29300.25,
                "stop_price": 29250.25,
                "target_price": 29400.25,
                "execution_lane": "capital_execution",
            },
        ),
        encoding="utf-8",
    )
    os.utime(pending_path, (stale_ts.timestamp(), stale_ts.timestamp()))

    adopted = sup._adopt_stale_shadow_pending_entry_if_needed(bot, now=datetime.now(UTC))  # noqa: SLF001

    assert adopted is False
    assert bot.open_position is None
    assert pending_path.exists() is True
    assert not (cfg.broker_router_pending_dir.parent / "shadow_adopted").exists()


def test_supervisor_blocks_recent_filled_result_when_broker_flat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "router" / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    monkeypatch.setattr(sup._router, "_get_broker_position_qty", lambda _bot: 0.0)  # noqa: SLF001

    signal_id = "mnq_futures_sage_flat"
    fill_dir = cfg.broker_router_pending_dir.parent / "fill_results"
    fill_dir.mkdir(parents=True, exist_ok=True)
    (fill_dir / f"{signal_id}_result.json").write_text(
        json.dumps(
            {
                "signal_id": signal_id,
                "bot_id": bot.bot_id,
                "venue": "ibkr",
                "request": {
                    "symbol": "MNQ",
                    "side": "BUY",
                    "qty": 1.0,
                    "price": 29300.25,
                    "reduce_only": False,
                },
                "result": {
                    "order_id": "OID-2",
                    "status": "FILLED",
                    "filled_qty": 1.0,
                    "avg_price": 29300.25,
                },
                "ts": datetime.now(UTC).isoformat(),
            },
        ),
        encoding="utf-8",
    )

    adopted = sup._adopt_broker_router_fill_if_needed(bot, now=datetime.now(UTC))  # noqa: SLF001
    reason = sup._broker_router_pending_entry_block_reason(bot, now=datetime.now(UTC))  # noqa: SLF001

    assert adopted is False
    assert bot.open_position is None
    assert reason.startswith("broker_router_filled_unadopted:")


def test_broker_router_stale_fill_reject_clears_without_recent_candidate(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "router" / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    bot.last_aggregation_reject_reason = "broker_router_filled_but_broker_flat"
    bot.last_aggregation_reject_at = datetime.now(UTC).isoformat()

    adopted = sup._adopt_broker_router_fill_if_needed(bot, now=datetime.now(UTC))  # noqa: SLF001

    assert adopted is False
    assert bot.last_aggregation_reject_reason == ""
    assert bot.last_aggregation_reject_at == ""


def test_broker_router_old_filled_flat_sidecar_does_not_reblock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "router" / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup._router, "_get_broker_position_qty", lambda _bot: 0.0)  # noqa: SLF001

    bot = BotInstance(
        bot_id="mym_sweep_reclaim",
        symbol="MYM1",
        strategy_kind="confluence_scorecard",
        direction="long",
        cash=50_000.0,
    )
    bot.last_aggregation_reject_reason = "broker_router_filled_but_broker_flat"
    bot.last_aggregation_reject_at = datetime.now(UTC).isoformat()

    signal_id = "mym_sweep_reclaim_old"
    fill_dir = cfg.broker_router_pending_dir.parent / "fill_results"
    fill_dir.mkdir(parents=True, exist_ok=True)
    stale_ts = (datetime.now(UTC) - timedelta(seconds=1800)).isoformat()
    (fill_dir / f"{signal_id}_result.json").write_text(
        json.dumps(
            {
                "signal_id": signal_id,
                "bot_id": bot.bot_id,
                "venue": "ibkr",
                "request": {
                    "symbol": "MYM",
                    "side": "BUY",
                    "qty": 1.0,
                    "price": 50147.0,
                    "reduce_only": False,
                },
                "result": {
                    "order_id": "OID-OLD",
                    "status": "FILLED",
                    "filled_qty": 1.0,
                    "avg_price": 50147.0,
                },
                "ts": stale_ts,
            },
        ),
        encoding="utf-8",
    )

    adopted = sup._adopt_broker_router_fill_if_needed(bot, now=datetime.now(UTC))  # noqa: SLF001

    assert adopted is False
    assert bot.last_aggregation_reject_reason == ""
    assert bot.last_aggregation_reject_at == ""


def test_direct_ibkr_open_without_fill_does_not_create_local_open_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    monkeypatch.setenv("ETA_PAPER_FUTURES_FLOOR", "1")
    monkeypatch.setattr(mod.l2hooks, "pre_trade_check", lambda *_args, **_kwargs: True)

    class FakeVenue:
        def place_order(self, _request):
            return object()

    fake_result = SimpleNamespace(
        status=SimpleNamespace(value="OPEN"),
        raw={"ibkr_order_id": 1467},
        filled_qty=0.0,
        avg_price=0.0,
        order_id="mbt_funding_basis_test",
        fees=0.0,
    )
    monkeypatch.setattr(mod, "_get_live_ibkr_venue", lambda: FakeVenue())
    monkeypatch.setattr(mod, "_run_on_live_ibkr_loop", lambda _awaitable, timeout=30.0: fake_result)

    cfg = mod.SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    cfg.state_dir = tmp_path / "state"
    router = mod.ExecutionRouter(cfg=cfg, bf_dir=tmp_path / "pending")
    bot = mod.BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="mnq_futures_sage_test",
        side="BUY",
        bar={"close": 29350.0, "high": 29360.0, "low": 29340.0, "open": 29345.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert "direct_ibkr_pending_order" in rec.note
    assert bot.open_position is None
    assert bot.n_entries == 0
    assert bot.consecutive_broker_rejects == 0
    assert not (cfg.state_dir / "bots" / "mnq_futures_sage" / "open_position.json").exists()


def test_env_file_loader_tolerates_non_utf8_bytes(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import _read_env_file_lines

    env_path = tmp_path / ".env"
    env_path.write_bytes(b"ETA_SUPERVISOR_FEED=composite\n# bad byte: \x9d\n")

    assert _read_env_file_lines(env_path)[0] == "ETA_SUPERVISOR_FEED=composite"


def test_supervisor_mutes_http_client_info_logs_by_default() -> None:
    import logging

    import eta_engine.scripts.jarvis_strategy_supervisor  # noqa: F401

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


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
        bot_id="test",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    bar = {"close": 100.0, "high": 101.0, "low": 99.0, "open": 99.5}
    rec = router.submit_entry(
        bot=bot,
        signal_id="sig1",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    assert rec is not None
    assert rec.side == "BUY"
    assert rec.symbol == "BTC"
    assert rec.qty > 0
    assert rec.fill_price > 99.0
    assert bot.open_position is not None
    assert bot.n_entries == 1


def test_persisted_open_position_includes_symbol(tmp_path: Path) -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="mnq_symbol_persist",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
        open_position={
            "side": "BUY",
            "qty": 1.0,
            "entry_price": 29_300.0,
            "entry_ts": "2026-05-13T16:00:00+00:00",
            "signal_id": "sig-symbol",
        },
    )

    router._persist_open_position(bot)

    path = cfg.state_dir / "bots" / "mnq_symbol_persist" / "open_position.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["symbol"] == "MNQ1"
    assert bot.open_position is not None
    assert bot.open_position["symbol"] == "MNQ1"


def test_load_persisted_open_positions_restores_bot_state(tmp_path: Path) -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    bot = BotInstance(
        bot_id="mnq_restore",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path, bots_ref=lambda: [bot])
    persisted = cfg.state_dir / "bots" / bot.bot_id / "open_position.json"
    persisted.parent.mkdir(parents=True, exist_ok=True)
    persisted.write_text(
        json.dumps(
            {
                "side": "BUY",
                "qty": 1.0,
                "entry_price": 29_300.0,
                "entry_ts": "2026-05-13T16:00:00+00:00",
                "signal_id": "sig-restore",
                "symbol": "MNQ1",
            }
        ),
        encoding="utf-8",
    )

    restored = router._load_persisted_open_positions()

    assert restored == 1
    assert bot.open_position is not None
    assert bot.open_position["signal_id"] == "sig-restore"
    assert bot.open_position["symbol"] == "MNQ1"


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
        bot_id="test",
        symbol="ETH",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    # Enter
    enter_bar = {"close": 100.0, "high": 101.0, "low": 99.0, "open": 99.5}
    router.submit_entry(
        bot=bot,
        signal_id="sig2",
        side="BUY",
        bar=enter_bar,
        size_mult=1.0,
    )
    # Exit at higher price (winning trade)
    exit_bar = {"close": 105.0, "high": 105.5, "low": 104.0, "open": 104.5}
    rec = router.submit_exit(bot=bot, bar=exit_bar)
    assert rec is not None
    assert rec.side == "SELL"
    assert rec.realized_r is not None
    assert rec.realized_r > 0  # winning trade
    assert bot.realized_pnl > 0
    assert bot.open_position is None
    assert bot.n_exits == 1


def test_supervisor_force_flattens_stale_supervisor_local_position(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._propagate_close = lambda *args, **kwargs: None  # type: ignore[method-assign]
    bot = BotInstance(
        bot_id="stale_local",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
        open_position={
            "side": "BUY",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": (datetime.now(UTC) - timedelta(seconds=7300)).isoformat(),
            "signal_id": "sig-stale",
            "bracket_stop": 90.0,
            "bracket_target": 130.0,
        },
    )
    bar = {
        "ts": datetime.now(UTC).isoformat(),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10,
    }

    sup._maybe_exit(bot, bar)

    assert bot.open_position is None
    assert bot.n_exits == 1


def test_supervisor_tightens_stale_supervisor_local_stop(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="stale_tighten",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
        open_position={
            "side": "BUY",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": (datetime.now(UTC) - timedelta(seconds=3700)).isoformat(),
            "signal_id": "sig-stale-tighten",
            "bracket_stop": 90.0,
            "bracket_target": 130.0,
        },
    )
    bar = {
        "ts": datetime.now(UTC).isoformat(),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10,
    }

    sup._maybe_exit(bot, bar)

    assert bot.open_position is not None
    assert bot.n_exits == 0
    assert bot.open_position["bracket_stop"] == 92.5
    assert bot.open_position["stale_tighten_prev_stop"] == 90.0
    assert bot.open_position["stale_tighten_factor"] == 0.75
    assert bot.open_position["stale_tighten_age_s"] >= 3600.0


def test_stale_tighten_never_loosens_existing_stop(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="stale_tighten_be",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
        open_position={
            "side": "BUY",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": (datetime.now(UTC) - timedelta(seconds=3700)).isoformat(),
            "signal_id": "sig-stale-tighten-be",
            "bracket_stop": 101.0,
            "bracket_target": 130.0,
        },
    )

    sup._maybe_tighten_stale_position(
        bot,
        bot.open_position,
        now=datetime.now(UTC),
    )

    assert bot.open_position is not None
    assert bot.open_position["bracket_stop"] == 101.0
    assert "stale_tighten_applied_at" not in bot.open_position


def test_stale_force_flatten_blocks_immediate_reentry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_STALE_FLATTEN_COOLDOWN_S", "900")
    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (False, "clear"),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.cfg.data_feed = "unit"
    sup._propagate_close = lambda *args, **kwargs: None  # type: ignore[method-assign]
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)

    def fail_if_reentry_consults_jarvis(**_kwargs):
        raise AssertionError("stale cooldown should block immediate re-entry")

    monkeypatch.setattr(sup, "_consult_jarvis", fail_if_reentry_consults_jarvis)
    bot = BotInstance(
        bot_id="stale_reentry",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
        open_position={
            "side": "BUY",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": (datetime.now(UTC) - timedelta(seconds=7300)).isoformat(),
            "signal_id": "sig-stale-reentry",
            "bracket_stop": 90.0,
            "bracket_target": 130.0,
        },
    )
    bar = {
        "ts": datetime.now(UTC).isoformat(),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10,
    }

    sup._maybe_exit(bot, bar)
    assert bot.open_position is None

    sup._maybe_enter(bot, bar)

    assert bot.open_position is None
    assert bot.last_aggregation_reject_reason.startswith("stale_force_flatten_cooldown:")
    assert bot.last_aggregation_reject_at


def test_paper_live_calendar_route_clears_pseudo_reject_after_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A successful paper-live route must not look blocked in heartbeat.

    The live-capital calendar returns target="paper" before 2026-07-08.
    That is a routing decision, not an entry rejection. If the broker-router
    submit succeeds, the heartbeat reject field must be cleared so operators
    do not read a successful paper order as "calendar blocked".
    """
    from unittest.mock import MagicMock

    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        FillRecord,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (False, "clear"),
    )
    monkeypatch.setattr(ca, "prop_entry_size_multiplier", lambda _bot_id: 1.0)
    monkeypatch.setattr(ca, "get_prop_guard_signal", lambda: "GO")
    monkeypatch.setattr(ca, "get_bot_lifecycle", lambda _bot_id: ca.LIFECYCLE_EVAL_PAPER)
    monkeypatch.setattr(
        ca,
        "resolve_execution_target",
        lambda _bot_id, prospective_loss_usd: (
            "paper",
            "live_capital_calendar_hold_until_2026-07-08: paper_live only",
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: "")

    verdict = MagicMock()
    verdict.is_blocked.return_value = False
    verdict.consolidated.final_verdict = "APPROVED"
    verdict.consolidated.confidence = 0.95
    verdict.final_size_multiplier = 1.0
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: verdict)

    fill = FillRecord(
        bot_id="mnq_futures_sage",
        signal_id="sig-paper",
        side="BUY",
        symbol="MNQ1",
        qty=1.0,
        fill_price=100.0,
        fill_ts="2026-05-15T01:00:00+00:00",
        paper=True,
        note="mode=paper_live",
    )
    submit_entry = MagicMock(return_value=fill)
    monkeypatch.setattr(sup._router, "submit_entry", submit_entry)

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert submit_entry.called
    assert bot.last_aggregation_reject_reason == ""
    assert bot.last_aggregation_reject_at == ""


def test_paper_live_pending_entry_does_not_update_cross_bot_exposure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        FillRecord,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setattr(daily_loss_killswitch, "is_killswitch_tripped", lambda: (False, "clear"))
    monkeypatch.setattr(ca, "prop_entry_size_multiplier", lambda _bot_id: 1.0)
    monkeypatch.setattr(ca, "get_prop_guard_signal", lambda: "GO")
    monkeypatch.setattr(ca, "get_bot_lifecycle", lambda _bot_id: ca.LIFECYCLE_EVAL_PAPER)
    monkeypatch.setattr(ca, "resolve_execution_target", lambda _bot_id, prospective_loss_usd: ("paper", "unit"))

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: "")
    persisted: list[list[dict]] = []
    monkeypatch.setattr(mod, "persist_open_positions", lambda rows: persisted.append(rows))

    verdict = MagicMock()
    verdict.is_blocked.return_value = False
    verdict.consolidated.final_verdict = "APPROVED"
    verdict.consolidated.confidence = 0.95
    verdict.final_size_multiplier = 1.0
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: verdict)

    fill = FillRecord(
        bot_id="mnq_futures_sage",
        signal_id="sig-paper",
        side="BUY",
        symbol="MNQ1",
        qty=1.0,
        fill_price=100.0,
        fill_ts="2026-05-15T01:00:00+00:00",
        paper=True,
        note="mode=paper_live;broker_router_pending_order",
    )
    submit_entry = MagicMock(return_value=fill)
    monkeypatch.setattr(sup._router, "submit_entry", submit_entry)

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    sup.bots.append(bot)

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert submit_entry.called
    assert sup._cross_bot_tracker.net_position("MNQ") == 0.0  # noqa: SLF001
    assert bot.last_aggregation_reject_reason == ""
    assert any(bot_id == "mnq_futures_sage" for bot_id, _signal_id in sup._sent_signals)  # noqa: SLF001
    assert persisted[-1] == []


def test_daily_kill_switch_block_is_visible_on_bot_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (True, "day_pnl=-1214.75 <= limit=-1000.00"),
    )
    monkeypatch.setenv("ETA_PAPER_LIVE_KILLSWITCH_MODE", "enforce")

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: (_ for _ in ()).throw(AssertionError))

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert bot.last_aggregation_reject_reason == "daily_kill_switch:day_pnl=-1214.75 <= limit=-1000.00"
    assert bot.last_aggregation_reject_at
    assert bot.execution_lane == "shadow_paper"
    assert bot.capital_gate_scope == "shadow_observe"
    assert bot.daily_loss_gate_mode == "enforce"


def test_session_gate_block_is_visible_on_bot_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import supervisor_session_wiring
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setattr(
        supervisor_session_wiring,
        "evaluate_pre_entry_gate",
        lambda *_args, **_kwargs: (False, "outside_rth"),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert bot.last_aggregation_reject_reason == "session_gate:outside_rth"
    assert bot.last_aggregation_reject_at


def test_session_gate_reason_clears_when_reallowed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import supervisor_session_wiring
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setattr(
        supervisor_session_wiring,
        "evaluate_pre_entry_gate",
        lambda *_args, **_kwargs: (True, ""),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: True)

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
        last_aggregation_reject_reason="session_gate:outside_rth",
        last_aggregation_reject_at="2026-05-15T01:00:00+00:00",
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:05:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert bot.last_aggregation_reject_reason == ""
    assert bot.last_aggregation_reject_at == ""


def test_paper_live_killswitch_advisory_allows_paper_soak_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        FillRecord,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_KILLSWITCH_MODE", "advisory")
    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (True, "day_pnl=-1214.75 <= limit=-1000.00"),
    )
    monkeypatch.setattr(ca, "prop_entry_size_multiplier", lambda _bot_id: 1.0)
    monkeypatch.setattr(ca, "get_prop_guard_signal", lambda: "GO")
    monkeypatch.setattr(ca, "get_bot_lifecycle", lambda _bot_id: ca.LIFECYCLE_EVAL_PAPER)
    monkeypatch.setattr(
        ca,
        "resolve_execution_target",
        lambda _bot_id, prospective_loss_usd: (
            "paper",
            "live_capital_calendar_hold_until_2026-07-08: paper_live only",
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.live_money_enabled = False
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: "")

    verdict = MagicMock()
    verdict.is_blocked.return_value = False
    verdict.consolidated.final_verdict = "APPROVED"
    verdict.consolidated.confidence = 0.95
    verdict.final_size_multiplier = 1.0
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: verdict)

    fill = FillRecord(
        bot_id="mnq_futures_sage",
        signal_id="sig-paper",
        side="BUY",
        symbol="MNQ1",
        qty=1.0,
        fill_price=100.0,
        fill_ts="2026-05-15T01:00:00+00:00",
        paper=True,
        note="mode=paper_live",
    )
    submit_entry = MagicMock(return_value=fill)
    monkeypatch.setattr(sup._router, "submit_entry", submit_entry)

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert submit_entry.called
    assert bot.last_aggregation_reject_reason == ""
    assert bot.last_aggregation_reject_at == ""
    assert bot.execution_lane == "shadow_paper"
    assert bot.daily_loss_gate_mode == "advisory"
    assert bot.daily_loss_gate_active is True
    assert "day_pnl=-1214.75" in bot.daily_loss_gate_reason


def test_shadow_paper_skips_prop_sleeve_cap_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        FillRecord,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PROP_SLEEVE_CAP_NASDAQ", "10")
    monkeypatch.setattr(daily_loss_killswitch, "is_killswitch_tripped", lambda: (False, ""))
    monkeypatch.setattr(ca, "prop_entry_size_multiplier", lambda _bot_id: 1.0)
    monkeypatch.setattr(ca, "get_prop_guard_signal", lambda: "GO")
    monkeypatch.setattr(ca, "get_bot_lifecycle", lambda _bot_id: ca.LIFECYCLE_EVAL_PAPER)
    monkeypatch.setattr(
        ca,
        "resolve_execution_target",
        lambda _bot_id, prospective_loss_usd: (
            "paper",
            "paper_shadow_only",
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.live_money_enabled = False
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: "")

    verdict = MagicMock()
    verdict.is_blocked.return_value = False
    verdict.consolidated.final_verdict = "APPROVED"
    verdict.consolidated.confidence = 0.95
    verdict.final_size_multiplier = 1.0
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: verdict)

    fill = FillRecord(
        bot_id="volume_profile_nq",
        signal_id="sig-paper",
        side="BUY",
        symbol="NQ1",
        qty=1.0,
        fill_price=100.0,
        fill_ts="2026-05-15T01:00:00+00:00",
        paper=True,
        note="mode=paper_live",
    )
    submit_entry = MagicMock(return_value=fill)
    monkeypatch.setattr(sup._router, "submit_entry", submit_entry)

    bot = BotInstance(
        bot_id="volume_profile_nq",
        symbol="NQ1",
        strategy_kind="volume_profile",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert submit_entry.called
    assert bot.last_aggregation_reject_reason == ""
    assert bot.execution_lane == "shadow_paper"
    assert bot.capital_gate_scope == "shadow_observe"


def test_prop_sleeve_cap_still_blocks_capital_execution_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PROP_SLEEVE_CAP_NASDAQ", "10")
    monkeypatch.setattr(daily_loss_killswitch, "is_killswitch_tripped", lambda: (False, ""))
    monkeypatch.setattr(ca, "prop_entry_size_multiplier", lambda _bot_id: 1.0)
    monkeypatch.setattr(ca, "get_prop_guard_signal", lambda: "GO")
    monkeypatch.setattr(ca, "get_bot_lifecycle", lambda _bot_id: ca.LIFECYCLE_APPROVED)
    monkeypatch.setattr(
        ca,
        "resolve_execution_target",
        lambda _bot_id, prospective_loss_usd: (
            "live",
            "prop_live_enabled",
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.live_money_enabled = True
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: "")

    verdict = MagicMock()
    verdict.is_blocked.return_value = False
    verdict.consolidated.final_verdict = "APPROVED"
    verdict.consolidated.confidence = 0.95
    verdict.final_size_multiplier = 1.0
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: verdict)

    submit_entry = MagicMock()
    monkeypatch.setattr(sup._router, "submit_entry", submit_entry)

    bot = BotInstance(
        bot_id="volume_profile_nq",
        symbol="NQ1",
        strategy_kind="volume_profile",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert submit_entry.called is False
    assert bot.last_aggregation_reject_reason.startswith("prop_sleeve_cap:")
    assert bot.execution_lane == "capital_execution"
    assert bot.capital_gate_scope == "prop_live"


def test_eval_live_lifecycle_blocks_prop_sleeve_when_lane_scope_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    import eta_engine.scripts.jarvis_strategy_supervisor as supervisor_mod
    from eta_engine.feeds import capital_allocator as ca
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PROP_SLEEVE_CAP_NASDAQ", "10")
    monkeypatch.setattr(daily_loss_killswitch, "is_killswitch_tripped", lambda: (False, ""))
    monkeypatch.setattr(ca, "prop_entry_size_multiplier", lambda _bot_id: 1.0)
    monkeypatch.setattr(ca, "get_prop_guard_signal", lambda: "GO")
    monkeypatch.setattr(ca, "get_bot_lifecycle", lambda _bot_id: ca.LIFECYCLE_EVAL_LIVE)
    monkeypatch.setattr(
        ca,
        "resolve_execution_target",
        lambda _bot_id, prospective_loss_usd: (
            "live",
            "eval_live_enabled",
        ),
    )
    monkeypatch.setattr(supervisor_mod, "capital_gate_scope_for_lane", lambda _lane: "unknown")

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.live_money_enabled = True
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    cfg.broker_router_pending_dir = tmp_path / "pending"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **_kwargs: "")

    verdict = MagicMock()
    verdict.is_blocked.return_value = False
    verdict.consolidated.final_verdict = "APPROVED"
    verdict.consolidated.confidence = 0.95
    verdict.final_size_multiplier = 1.0
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: verdict)

    submit_entry = MagicMock()
    monkeypatch.setattr(sup._router, "submit_entry", submit_entry)

    bot = BotInstance(
        bot_id="volume_profile_nq",
        symbol="NQ1",
        strategy_kind="volume_profile",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert submit_entry.called is False
    assert bot.last_aggregation_reject_reason.startswith("prop_sleeve_cap:")
    assert bot.execution_lane == "capital_execution"
    assert bot.capital_gate_scope == "unknown"


def test_paper_live_killswitch_advisory_keeps_live_money_hard_stopped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_KILLSWITCH_MODE", "advisory")
    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (True, "day_pnl=-1214.75 <= limit=-1000.00"),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.live_money_enabled = True
    cfg.data_feed = "unit"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "_strategy_readiness_allows_entry", lambda _bot: True)
    monkeypatch.setattr(sup, "_enforce_daily_loss_cap", lambda _bot, now: False)
    monkeypatch.setattr(sup, "_consult_jarvis", lambda **_kwargs: (_ for _ in ()).throw(AssertionError))

    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )

    sup._maybe_enter(
        bot,
        {
            "ts": "2026-05-15T01:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
    )

    assert bot.last_aggregation_reject_reason == "daily_kill_switch:day_pnl=-1214.75 <= limit=-1000.00"
    assert bot.last_aggregation_reject_at


def test_router_paper_live_direct_route_skips_pending_by_default(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    bar = {"close": 100.0}
    router.submit_entry(
        bot=bot,
        signal_id="s1",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    pending = tmp_path / "paperlive.pending_order.json"
    assert not pending.exists()


def test_router_paper_live_broker_router_route_writes_pending(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    bar = {"close": 100.0}
    router.submit_entry(
        bot=bot,
        signal_id="s1",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    pending = tmp_path / "paperlive.pending_order.json"
    assert pending.exists()


def test_router_paper_live_broker_router_bypasses_allowed_symbols(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """ETA_PAPER_LIVE_ALLOWED_SYMBOLS is now a direct_ibkr-only gate.

    The broker_router route consults the routing yaml as the source of
    truth for which (bot, symbol) pairs go where. Applying the
    allowlist on broker_router would block crypto bots whose symbols
    aren't in the operator-curated futures set — which is exactly the
    bug we hit live (BTC entries from btc_optimized routed to alpaca
    were rejected because BTC wasn't in the futures allowlist).
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="crypto_live_paused",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="s1",
        side="BUY",
        bar={"close": 100.0},
        size_mult=1.0,
    )

    # broker_router route writes the pending_order JSON regardless of
    # the legacy allowlist; routing yaml decides where it actually goes.
    assert rec is not None
    assert (tmp_path / "crypto_live_paused.pending_order.json").exists()


def test_router_paper_live_direct_ibkr_still_honors_allowed_symbols(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The allowlist remains a hard gate on the direct_ibkr path."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="crypto_live_paused",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="s1",
        side="BUY",
        bar={"close": 100.0},
        size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None


def test_router_paper_live_order_entry_hold_blocks_before_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    hold_path = tmp_path / "order_entry_hold.json"
    hold_path.write_text(
        '{"active": true, "reason": "manual_flatten"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD_PATH", str(hold_path))
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="held",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="s-held",
        side="BUY",
        bar={"close": 100.0},
        size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None
    assert bot.n_entries == 0
    assert not (tmp_path / "held.pending_order.json").exists()


def test_router_paper_live_direct_reject_rolls_back_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50000.0,
    )

    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: object())
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_args, **_kwargs: OrderResult(
            order_id="sig-reject",
            status=OrderStatus.REJECTED,
            raw={
                "ibkr_order_id": 30,
                "reason": "IBKR submission unconfirmed after confirm window",
            },
        ),
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-reject",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None
    assert bot.n_entries == 0


def test_router_paper_live_direct_order_carries_reference_price(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus, OrderType

    class CapturingVenue:
        def __init__(self) -> None:
            self.request = None

        def place_order(self, request):
            self.request = request
            return object()

    venue = CapturingVenue()
    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "100000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "100000")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: venue)
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_args, **_kwargs: OrderResult(
            order_id="sig-open",
            status=OrderStatus.OPEN,
            raw={"ibkr_order_id": 42},
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-open",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert venue.request is not None
    assert venue.request.order_type is OrderType.MARKET
    assert venue.request.price == rec.fill_price
    assert venue.request.stop_price is not None
    assert venue.request.target_price is not None


def test_router_paper_live_direct_route_uses_helper_built_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.scripts.supervisor_entry_helpers import DirectIbkrEntryPlan
    from eta_engine.venues.base import OrderResult, OrderStatus

    class CapturingVenue:
        def __init__(self) -> None:
            self.request = None

        def place_order(self, request):
            self.request = request
            return object()

    venue = CapturingVenue()
    sentinel_request = object()
    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "100000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "100000")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: venue)
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_args, **_kwargs: OrderResult(
            order_id="sig-open",
            status=OrderStatus.OPEN,
            raw={"ibkr_order_id": 42},
        ),
    )
    monkeypatch.setattr(
        supervisor.supervisor_entry_helpers,
        "build_direct_ibkr_entry_plan",
        lambda **_kwargs: DirectIbkrEntryPlan(
            request=sentinel_request,
            ref_price=28250.0,
            stop_price=28200.0,
            target_price=28350.0,
            bracket_src="helper",
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive_helper_request",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-open",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert "direct_ibkr_pending_order" in rec.note
    assert venue.request is sentinel_request


def test_router_paper_live_direct_order_uses_result_finalizer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    class CapturingVenue:
        def __init__(self) -> None:
            self.request = None

        def place_order(self, request):
            self.request = request
            return object()

    venue = CapturingVenue()
    result = OrderResult(
        order_id="sig-open",
        status=OrderStatus.OPEN,
        raw={"ibkr_order_id": 42},
    )
    finalizer_calls = []

    def _finalize(**kwargs):
        finalizer_calls.append(kwargs)
        return supervisor.supervisor_entry_helpers.DirectIbkrEntryOutcome(
            action="pending",
            reason="n/a",
            filled_qty=0.0,
        )

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "100000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "100000")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: venue)
    monkeypatch.setattr(supervisor, "_run_on_live_ibkr_loop", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        supervisor.supervisor_entry_helpers,
        "finalize_direct_ibkr_entry_result",
        _finalize,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-open",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert len(finalizer_calls) == 1
    call = finalizer_calls[0]
    assert call["bot"] is bot
    assert call["rec"] is rec
    assert call["result"] is result
    assert call["ref_price"] == rec.fill_price
    assert call["stop_price"] is not None
    assert call["target_price"] is not None


def test_router_paper_live_filled_entry_records_l2_fill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    class CapturingVenue:
        def __init__(self) -> None:
            self.request = None

        def place_order(self, request):
            self.request = request
            return object()

    venue = CapturingVenue()
    fill_calls = []
    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "100000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "100000")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)
    monkeypatch.setattr(
        supervisor.l2hooks,
        "record_fill",
        lambda **kwargs: fill_calls.append(kwargs),
    )
    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: venue)
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_args, **_kwargs: OrderResult(
            order_id="fallback-order-id",
            status=OrderStatus.FILLED,
            filled_qty=1.0,
            avg_price=28254.5,
            fees=1.23,
            raw={"ibkr_order_id": 77},
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="paperlive_filled",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-filled",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert fill_calls == [
        {
            "signal_id": "sig-filled",
            "broker_exec_id": "77",
            "exit_reason": "ENTRY",
            "side": "LONG",
            "actual_fill_price": 28254.5,
            "qty_filled": 1,
            "commission_usd": 1.23,
            "intended_price": rec.fill_price,
            "tick_size": 0.25,
        },
    ]
    assert bot.open_position is not None
    assert bot.open_position["broker_bracket"] is True


def test_router_paper_live_futures_floor_reaches_broker_with_small_cash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    class CapturingVenue:
        def __init__(self) -> None:
            self.request = None

        def place_order(self, request):
            self.request = request
            return object()

    venue = CapturingVenue()
    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "500")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "5000")
    monkeypatch.setenv("ETA_PAPER_FUTURES_FLOOR", "1")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: venue)
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_args, **_kwargs: OrderResult(
            order_id="sig-floor",
            status=OrderStatus.OPEN,
            raw={"ibkr_order_id": 43},
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="mnq_floor",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-floor",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert rec.qty == 1.0
    assert venue.request is not None
    assert venue.request.qty == 1.0
    assert venue.request.price == rec.fill_price


def test_router_paper_sim_futures_floor_records_local_fill_with_50k_cash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "500")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "5000")
    monkeypatch.setenv("ETA_PAPER_FUTURES_FLOOR", "1")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="mnq_floor_sim",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-floor-sim",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    assert rec.qty == 1.0
    assert rec.paper is True
    assert bot.open_position is not None


def test_write_pending_order_includes_brackets_when_available(
    tmp_path: Path,
) -> None:
    """The supervisor's pending JSON must carry stop_price + target_price.

    submit_entry() populates ``bot.open_position['bracket_stop']`` and
    ``bot.open_position['bracket_target']`` at entry time; the
    pending-order writer must echo those into the JSON so the
    broker_router can pass them to the venue's OrderRequest. Without
    them the venue layer rejects every entry as naked.
    """
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "broker_router"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="bracket_bot",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    bar = {"close": 100.0, "high": 101.0, "low": 99.0, "open": 99.5}
    router.submit_entry(
        bot=bot,
        signal_id="bracket-1",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    pending = tmp_path / "bracket_bot.pending_order.json"
    assert pending.exists()
    payload = json.loads(pending.read_text(encoding="utf-8"))
    # Schema must now carry the bracket fields.
    assert "stop_price" in payload
    assert "target_price" in payload
    # Broker-router pending mode must carry bracket fields in the wire JSON
    # while leaving bot.open_position empty until broker fill evidence arrives.
    assert payload["stop_price"] is not None
    assert payload["target_price"] is not None
    assert bot.open_position is None


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
        bot_id="livelock",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
    )
    rec = router.submit_entry(
        bot=bot,
        signal_id="s1",
        side="BUY",
        bar={"close": 100.0},
        size_mult=1.0,
    )
    assert rec is None
    assert bot.open_position is None


# ─── BotInstance ─────────────────────────────────────────────────


def test_bot_instance_serializable() -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    bot = BotInstance(
        bot_id="x",
        symbol="MNQ",
        strategy_kind="orb",
        direction="long",
        cash=5000.0,
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
    for k in (
        "ETA_SUPERVISOR_BOTS",
        "ETA_SUPERVISOR_FEED",
        "ETA_SUPERVISOR_MODE",
        "ETA_SUPERVISOR_STATE_DIR",
        "ETA_LIVE_MONEY",
    ):
        os.environ.pop(k, None)
    cfg = SupervisorConfig()
    assert cfg.mode == "paper_sim"
    assert cfg.data_feed == "mock"
    assert cfg.live_money_enabled is False
    assert cfg.tick_s > 0


# ─── Supervisor (loop integration, fast smoke) ────────────────────


def test_config_mock_feed_uses_isolated_default_state_dir(monkeypatch) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import SupervisorConfig

    monkeypatch.delenv("ETA_SUPERVISOR_STATE_DIR", raising=False)
    monkeypatch.setenv("ETA_SUPERVISOR_FEED", "mock")
    monkeypatch.setenv("ETA_SUPERVISOR_MODE", "paper_sim")

    cfg = SupervisorConfig()

    assert cfg.state_dir == (supervisor.workspace_roots.ETA_RUNTIME_STATE_DIR / "jarvis_intel" / "supervisor_mock")


def test_config_explicit_state_dir_overrides_mock_isolation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import SupervisorConfig

    monkeypatch.setenv("ETA_SUPERVISOR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ETA_SUPERVISOR_FEED", "mock")
    monkeypatch.setenv("ETA_SUPERVISOR_MODE", "paper_sim")

    cfg = SupervisorConfig()

    assert cfg.state_dir == tmp_path


def test_config_paper_live_composite_uses_canonical_state_dir(monkeypatch) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import SupervisorConfig

    monkeypatch.delenv("ETA_SUPERVISOR_STATE_DIR", raising=False)
    monkeypatch.setenv("ETA_SUPERVISOR_FEED", "composite")
    monkeypatch.setenv("ETA_SUPERVISOR_MODE", "paper_live")

    cfg = SupervisorConfig()

    assert cfg.state_dir == supervisor.workspace_roots.ETA_JARVIS_SUPERVISOR_STATE_DIR


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


def test_run_forever_writes_start_heartbeat_before_tick(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.core import kill_switch_latch as latch_mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class AllowLatch:
        def boot_allowed(self) -> tuple[bool, str]:
            return True, "armed"

    monkeypatch.setattr(latch_mod, "KillSwitchLatch", lambda _path: AllowLatch())
    monkeypatch.setattr(latch_mod, "default_path", lambda: tmp_path / "latch.json")

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.tick_s = 999
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "load_bots", lambda: 1)
    monkeypatch.setattr(sup, "bootstrap_jarvis", lambda: True)
    monkeypatch.setattr(sup, "reconcile_with_broker", lambda: None)

    observed = {"heartbeat_exists": False}

    def fake_tick(_tick_count: int) -> None:
        observed["heartbeat_exists"] = (cfg.state_dir / "heartbeat.json").exists()
        sup._stopped = True
        sup._stop_event.set()

    monkeypatch.setattr(sup, "_tick_once", fake_tick)

    assert sup.run_forever() == 0
    assert observed["heartbeat_exists"] is True


def test_reconcile_clears_stale_futures_roots_after_successful_ibkr_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    class FakeTracker:
        def __init__(self) -> None:
            self.state = {"BTC": 0.5, "MCL": 1.0, "MET": -0.25, "MYM": 1.0, "MNQ": 3.0}
            self.last_call: dict[str, object] | None = None

        def snapshot(self) -> dict[str, float]:
            return dict(self.state)

        def resync_from_broker(
            self,
            *,
            by_root,
            clear_missing_roots=None,
        ) -> None:
            clear_missing_roots = set(clear_missing_roots or ())
            for root, qty in by_root.items():
                self.state[str(root)] = float(qty)
            for root in clear_missing_roots:
                if root not in by_root:
                    self.state[str(root)] = 0.0
            self.last_call = {
                "by_root": dict(by_root),
                "clear_missing_roots": clear_missing_roots,
            }

    class FakeVenue:
        def get_positions(self):
            return object()

    cfg = mod.SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.state_dir = tmp_path / "state"
    sup = mod.JarvisStrategySupervisor(cfg=cfg)
    sup.bots = [
        mod.BotInstance(
            bot_id="mnq_futures_sage",
            symbol="MNQ1",
            strategy_kind="orb_sage_gated",
            direction="long",
            cash=50_000.0,
            open_position={
                "side": "BUY",
                "qty": 1.0,
                "entry_price": 29583.75,
            },
        )
    ]
    fake_tracker = FakeTracker()
    sup._cross_bot_tracker = fake_tracker  # noqa: SLF001

    monkeypatch.setattr(mod, "_get_live_ibkr_venue", lambda: FakeVenue())
    monkeypatch.setattr(
        mod,
        "_run_on_live_ibkr_loop",
        lambda _awaitable, timeout=10.0: [{"symbol": "MNQ", "position": 1.0}],
    )

    findings = sup.reconcile_with_broker()

    assert findings["matched"] == 1
    assert findings["brokers_queried"] == ["ibkr"]
    assert fake_tracker.last_call is not None
    clear_missing_roots = fake_tracker.last_call["clear_missing_roots"]
    assert isinstance(clear_missing_roots, set)
    assert {"MCL", "MET", "MYM", "MNQ"}.issubset(clear_missing_roots)
    assert "BTC" not in clear_missing_roots
    assert fake_tracker.state["BTC"] == 0.5
    assert fake_tracker.state["MCL"] == 0.0
    assert fake_tracker.state["MET"] == 0.0
    assert fake_tracker.state["MYM"] == 0.0
    assert fake_tracker.state["MNQ"] == 1.0


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
    sup.bots.append(
        BotInstance(
            bot_id="hb-test",
            symbol="BTC",
            strategy_kind="x",
            direction="long",
            cash=5000.0,
        )
    )
    sup._write_heartbeat(42)
    hb_file = cfg.state_dir / "heartbeat.json"
    assert hb_file.exists()
    payload = json.loads(hb_file.read_text(encoding="utf-8"))
    assert payload["tick_count"] == 42
    assert payload["n_bots"] == 1
    assert payload["bots"][0]["bot_id"] == "hb-test"


def test_supervisor_open_position_heartbeat_includes_latest_mark(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class OneBarFeed:
        def get_bar(self, _symbol: str) -> dict:
            return {
                "ts": "2026-05-08T00:00:00+00:00",
                "open": 100.0,
                "high": 106.0,
                "low": 99.0,
                "close": 105.0,
                "volume": 10,
            }

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.mode = "paper_live"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.feed = OneBarFeed()
    monkeypatch.setattr(JarvisStrategySupervisor, "_IS_REAL_BAR_FN", lambda _bar: True)
    monkeypatch.setattr(sup, "_maybe_exit", lambda _bot, _bar: None)
    bot = BotInstance(
        bot_id="btc_mark_test",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
        open_position={
            "side": "BUY",
            "qty": 0.05,
            "entry_price": 100.0,
            "bracket_stop": 95.0,
            "bracket_target": 110.0,
        },
    )
    sup.bots.append(bot)

    sup._tick_bot(bot, 1)
    sup._write_heartbeat(1)

    payload = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    pos = payload["bots"][0]["open_position"]
    assert pos["mark_price"] == 105.0
    assert pos["last_price"] == 105.0
    assert pos["last_bar_high"] == 106.0
    assert pos["last_bar_low"] == 99.0
    assert payload["bots"][0]["last_bar_close"] == 105.0


def test_tick_once_preserves_fractional_qty_for_l2_persist(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    captured: dict[str, list[dict]] = {}

    def fake_persist(positions: list[dict]) -> None:
        captured["positions"] = positions

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(mod, "persist_open_positions", fake_persist)
    monkeypatch.setattr(sup, "_tick_bot", lambda _bot, _tick_count: None)
    sup.bots.append(
        BotInstance(
            bot_id="mbt_fractional_paper",
            symbol="MBT1",
            strategy_kind="x",
            direction="long",
            cash=50_000.0,
            open_position={
                "side": "BUY",
                "qty": 0.125,
                "entry_price": 80_000.0,
                "entry_ts": "2026-05-13T16:00:00+00:00",
                "signal_id": "sig-mbt",
            },
        )
    )

    sup._tick_once(1)

    assert captured["positions"][0]["qty"] == 0.125


def test_tick_bot_runs_sage_health_probe_without_caching_last_report(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class OneBarFeed:
        def get_bar(self, _symbol: str) -> dict:
            return {
                "ts": "2026-05-08T00:00:00+00:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10,
            }

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.feed = OneBarFeed()
    monkeypatch.setenv("ETA_SAGE_HEALTH_PROBE_INTERVAL_S", "60")
    monkeypatch.setattr(JarvisStrategySupervisor, "_IS_REAL_BAR_FN", lambda _bar: True)
    monkeypatch.setattr(sup, "_maybe_enter", lambda _bot, _bar: None)
    monkeypatch.setattr(sup, "_maybe_exit", lambda _bot, _bar: None)

    calls: list[bool] = []

    def fake_consult(_bot, _bar, _side, _entry_price, *, cache_last=True):
        calls.append(cache_last)
        return object()

    monkeypatch.setattr(sup, "_consult_sage_for_bot", fake_consult)
    bot = BotInstance(
        bot_id="mnq_probe",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    sup.bots.append(bot)

    sup._tick_bot(bot, 1)

    assert calls == [False]
    assert bot.bot_id in sup._sage_health_probe_last_ts


def test_tick_bot_sage_health_probe_respects_interval(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class OneBarFeed:
        def get_bar(self, _symbol: str) -> dict:
            return {
                "ts": "2026-05-08T00:00:00+00:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10,
            }

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.feed = OneBarFeed()
    monkeypatch.setenv("ETA_SAGE_HEALTH_PROBE_INTERVAL_S", "300")
    monkeypatch.setattr(JarvisStrategySupervisor, "_IS_REAL_BAR_FN", lambda _bar: True)
    monkeypatch.setattr(sup, "_maybe_enter", lambda _bot, _bar: None)
    monkeypatch.setattr(sup, "_maybe_exit", lambda _bot, _bar: None)

    calls: list[bool] = []

    def fake_consult(_bot, _bar, _side, _entry_price, *, cache_last=True):
        calls.append(cache_last)
        return object()

    monkeypatch.setattr(sup, "_consult_sage_for_bot", fake_consult)
    bot = BotInstance(
        bot_id="mnq_probe",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
    )
    sup._sage_health_probe_last_ts[bot.bot_id] = datetime.now(UTC)
    sup.bots.append(bot)

    sup._tick_bot(bot, 1)

    assert calls == []


def test_tick_once_runs_background_sage_health_probe_from_latest_real_mark(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setenv("ETA_SAGE_HEALTH_PROBE_INTERVAL_S", "60")
    monkeypatch.setattr(sup, "_tick_bot", lambda _bot, _tick_count: None)

    calls: list[tuple[str, bool, float]] = []

    def fake_consult(_bot, _bar, _side, entry_price, *, cache_last=True):
        calls.append((_bot.bot_id, cache_last, entry_price))
        return object()

    monkeypatch.setattr(sup, "_consult_sage_for_bot", fake_consult)
    bot = BotInstance(
        bot_id="mnq_probe_background",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
        last_bar_ts="2026-05-08T00:00:00+00:00",
        last_bar_close=100.5,
        last_bar_high=101.0,
        last_bar_low=99.0,
    )
    sup.bots.append(bot)

    sup._tick_once(1)

    assert calls == [("mnq_probe_background", False, 100.5)]
    assert bot.bot_id in sup._sage_health_probe_last_ts


def test_tick_once_refreshes_main_heartbeat_between_bots(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    heartbeat_ticks: list[int] = []
    monkeypatch.setattr(sup, "_write_heartbeat", lambda tick_count: heartbeat_ticks.append(tick_count))
    monkeypatch.setattr(sup, "_run_background_sage_health_probes", lambda *, now: None)
    monkeypatch.setattr("eta_engine.scripts.jarvis_strategy_supervisor.persist_open_positions", lambda _rows: None)

    sup.bots.extend(
        [
            BotInstance(bot_id="bot_a", symbol="MNQ1", strategy_kind="x", direction="long", cash=50_000.0),
            BotInstance(bot_id="bot_b", symbol="NQ1", strategy_kind="x", direction="long", cash=50_000.0),
        ]
    )

    seen: list[str] = []

    def fake_tick_bot(bot, _tick_count):
        seen.append(bot.bot_id)

    monkeypatch.setattr(sup, "_tick_bot", fake_tick_bot)

    sup._tick_once(7)

    assert seen == ["bot_a", "bot_b"]
    assert heartbeat_ticks == [7, 7]


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


def test_supervisor_blocks_paper_live_entries_when_strategy_readiness_disallows(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
                "generated_at": "2026-05-08T14:00:00+00:00",
                "source": "bot_strategy_readiness",
                "summary": {"can_paper_trade": 0, "launch_lanes": {"research": 1}},
                "rows": [
                    {
                        "bot_id": "mbt_funding_basis",
                        "strategy_id": "mbt_funding_basis_v1",
                        "launch_lane": "research",
                        "data_status": "ready",
                        "promotion_status": "research_candidate",
                        "can_paper_trade": False,
                        "can_live_trade": False,
                        "next_action": "Continue research retest before promotion.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod.workspace_roots, "ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", readiness)

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mbt_funding_basis",
        symbol="MBT1",
        strategy_kind="mbt_funding_basis",
        direction="long",
        cash=5000.0,
    )

    assert sup._strategy_readiness_allows_entry(bot) is False  # noqa: SLF001
    assert bot.last_aggregation_reject_reason == "strategy_readiness_block:research"
    assert bot.last_aggregation_reject_at


def test_supervisor_loads_exit_watch_bot_without_entry_permission(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path / "state"
    cfg.bots_env = "volume_profile_mnq"
    cfg.exit_watch_bots_env = "mbt_funding_basis"
    sup = JarvisStrategySupervisor(cfg=cfg)

    sup.load_bots()

    bots = {bot.bot_id: bot for bot in sup.bots}
    assert set(bots) == {"volume_profile_mnq", "mbt_funding_basis"}
    assert bots["volume_profile_mnq"].entry_enabled is True
    assert bots["mbt_funding_basis"].entry_enabled is False
    assert bots["mbt_funding_basis"].entry_disabled_reason == "exit_watch_only"


def test_supervisor_load_bots_carries_registry_extras_for_futures_sage_lanes(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path / "state"
    cfg.bots_env = "mnq_futures_sage,nq_futures_sage"
    sup = JarvisStrategySupervisor(cfg=cfg)

    sup.load_bots()

    bots = {bot.bot_id: bot for bot in sup.bots}
    assert bots["mnq_futures_sage"].registry_extras["partial_profit_enabled"] is False
    assert bots["mnq_futures_sage"].partial_profit_enabled is False
    assert bots["nq_futures_sage"].registry_extras["partial_profit_enabled"] is False
    assert bots["nq_futures_sage"].partial_profit_enabled is False


def test_partial_profit_respects_bot_scoped_disable(monkeypatch, tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PARTIAL_PROFIT_ENABLED", "true")
    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    calls = {"submit_exit": 0}

    def _submit_exit(*, bot, bar):  # noqa: ARG001
        calls["submit_exit"] += 1
        return None

    monkeypatch.setattr(sup._router, "submit_exit", _submit_exit)
    bot = BotInstance(
        bot_id="mnq_futures_sage",
        symbol="MNQ1",
        strategy_kind="orb_sage_gated",
        direction="long",
        cash=50_000.0,
        registry_extras={"partial_profit_enabled": False},
        open_position={
            "entry_price": 100.0,
            "bracket_stop": 99.0,
            "qty": 1.0,
            "side": "BUY",
        },
    )
    before = dict(bot.open_position)

    sup._maybe_take_partial_profit(
        bot,
        bot.open_position,
        {"close": 101.25, "high": 101.25, "low": 100.5},
    )

    assert calls["submit_exit"] == 0
    assert bot.open_position == before


def test_supervisor_heartbeat_exposes_effective_partial_profit_flag(tmp_path: Path) -> None:
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path / "state"
    cfg.bots_env = "mnq_futures_sage"
    sup = JarvisStrategySupervisor(cfg=cfg)

    sup.load_bots()
    sup._write_heartbeat(1)  # noqa: SLF001

    payload = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    bot = next(row for row in payload["bots"] if row["bot_id"] == "mnq_futures_sage")
    assert bot["partial_profit_enabled"] is False
    assert "registry_extras" not in bot


def test_supervisor_heartbeat_per_bot_mode_inherits_cfg_mode(tmp_path: Path) -> None:
    """Regression: every per-bot heartbeat dict must carry ``mode`` field
    sourced from cfg.mode. Without this, the dashboard bridge falls back
    to a hardcoded ``paper_sim`` and all 52 bots show paper_sim despite
    the supervisor running paper_live. See PAPER_LIVE_ROUTING_GAP.md.
    """
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.mode = "paper_live"
    sup = JarvisStrategySupervisor(cfg=cfg)
    # Three bots, simulating the multi-bot fleet.
    for bot_id in ("alpha", "beta", "gamma"):
        sup.bots.append(
            BotInstance(
                bot_id=bot_id,
                symbol="BTC",
                strategy_kind="x",
                direction="long",
                cash=5000.0,
            )
        )
    sup._write_heartbeat(1)
    payload = json.loads(
        (cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"),
    )
    # Top-level cfg.mode pinned through to heartbeat.
    assert payload["mode"] == "paper_live"
    # Every per-bot dict must carry the same mode (no silent paper_sim).
    assert len(payload["bots"]) == 3
    for bot in payload["bots"]:
        assert bot["mode"] == "paper_live", (
            f"bot {bot['bot_id']} reports mode={bot.get('mode')!r}, expected paper_live (cfg.mode inheritance broken)"
        )


def test_supervisor_heartbeat_includes_code_revision(tmp_path: Path) -> None:
    """Heartbeat must identify the exact supervisor code the process loaded."""
    import json

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)

    sup._write_heartbeat(3)

    payload = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    revision = payload["code_revision"]
    assert revision["head"]
    assert revision["head_short"] == revision["head"][:7]
    assert revision["repo_root"].endswith("eta_engine")
    assert revision["captured_at"]


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
    sup.bots.append(
        BotInstance(
            bot_id="ctx-test",
            symbol="BTC",
            strategy_kind="x",
            direction="long",
            cash=5000.0,
        )
    )
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
        rationale="ctx smoke",
        side="buy",
        qty=1.0,
        symbol="BTC",
    )
    # Should NOT raise "JarvisAdmin needs either an engine or an
    # explicit ctx" -- the whole point of the synthetic ctx
    resp = admin.request_approval(req, ctx=ctx)
    assert resp is not None
    assert resp.verdict is not None


def test_supervisor_synthetic_ctx_treats_paper_killswitch_advisory_as_inactive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import daily_loss_killswitch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_KILLSWITCH_MODE", "advisory")
    monkeypatch.setattr(
        daily_loss_killswitch,
        "is_killswitch_tripped",
        lambda: (True, "day_pnl=-1214.75 <= limit=-1000.00"),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.live_money_enabled = False
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="ctx-test",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    sup.bots.append(bot)

    ctx = sup._build_synthetic_ctx(bot)

    assert ctx is not None
    assert ctx.journal.kill_switch_active is False


# ─── LiveIbkrVenue dedicated-loop-thread dispatcher ───────────────


def test_supervisor_builds_sage_context_with_depth_and_peer_returns(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class FakeMarketContext:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mnq_primary",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    peer = BotInstance(
        bot_id="nq_peer",
        symbol="NQ1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    other = BotInstance(
        bot_id="btc_peer",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    for idx, close in enumerate([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], start=1):
        peer.sage_bars.append({"close": close, "ts": f"p{idx}"})
        other.sage_bars.append({"close": close, "ts": f"o{idx}"})
    sup.bots.extend([bot, peer, other])
    monkeypatch.setattr(
        mod,
        "_latest_depth_snapshot",
        lambda _symbol: {
            "bids": [{"size": 12}, {"size": 8}, {"size": 5}],
            "asks": [{"size": 5}, {"size": 5}, {"size": 5}],
        },
    )

    ctx = sup._build_sage_context(
        bot,
        bars=[{"close": 105.0}],
        side="long",
        entry_price=105.0,
        market_context_cls=FakeMarketContext,
    )

    assert ctx.kwargs["symbol"] == "MNQ1"
    assert ctx.kwargs["instrument_class"] == "futures"
    assert ctx.kwargs["account_equity_usd"] == 50_000.0
    assert ctx.kwargs["order_book_imbalance"] == 0.25
    assert "NQ1" in ctx.kwargs["peer_returns"]
    assert "BTC" not in ctx.kwargs["peer_returns"]


def test_supervisor_builds_sage_context_with_crypto_telemetry(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as mod
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    class FakeMarketContext:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mbt_basis",
        symbol="MBT1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    sup.bots.append(bot)

    monkeypatch.setattr(
        mod,
        "_latest_depth_snapshot",
        lambda _symbol: {
            "book_imbalance": 0.4,
            "cumulative_delta": 12.5,
        },
    )
    monkeypatch.setattr(
        JarvisStrategySupervisor,
        "_load_onchain_payload",
        lambda self, _bot: {"sopr": 1.02, "funding_rate_bps": -3.0},
    )
    monkeypatch.setattr(
        JarvisStrategySupervisor,
        "_load_funding_payload",
        lambda self, _bot, **_kwargs: {
            "funding_rate_bps": -3.0,
            "perp_spot_basis_pct": 0.45,
            "annualized_yield_pct": 11.0,
        },
    )

    ctx = sup._build_sage_context(
        bot,
        bars=[{"close": 105.0, "ts": "2026-05-15T03:00:00+00:00"}],
        side="long",
        entry_price=105.0,
        market_context_cls=FakeMarketContext,
    )

    assert ctx.kwargs["order_book_imbalance"] == 0.4
    assert ctx.kwargs["cumulative_delta"] == 12.5
    assert ctx.kwargs["onchain"] == {"sopr": 1.02, "funding_rate_bps": -3.0}
    assert ctx.kwargs["funding"]["perp_spot_basis_pct"] == 0.45
    assert ctx.kwargs["funding"]["annualized_yield_pct"] == 11.0


def test_supervisor_load_onchain_payload_uses_contract_alias_fallback(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="mbt_basis",
        symbol="MBT1",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )

    calls: list[str] = []

    def fake_fetch(symbol: str) -> dict[str, object]:
        calls.append(symbol)
        if symbol == "MBT":
            return {"sopr": 0.99}
        return {}

    import eta_engine.brain.jarvis_v3.sage.onchain_fetcher as fetcher

    monkeypatch.setattr(fetcher, "fetch_onchain", fake_fetch)
    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.onchain_enricher.current_snapshot",
        lambda *args, **kwargs: None,
    )

    payload = sup._load_onchain_payload(bot)

    assert payload == {"sopr": 0.99}
    assert calls[:2] == ["MBT1", "MBT"]


def test_preferred_basis_spot_csv_prefers_freshest_workspace_feed(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    workspace_root = tmp_path / "workspace"
    dashboard_dir = workspace_root / "data"
    history_dir = workspace_root / "data" / "crypto" / "history"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)

    dashboard = dashboard_dir / "BTC_5m.csv"
    history = history_dir / "BTC_5m.csv"
    dashboard.write_text("dashboard", encoding="utf-8")
    history.write_text("history", encoding="utf-8")

    import os
    import time

    now = time.time()
    os.utime(history, (now - 3600, now - 3600))
    os.utime(dashboard, (now, now))

    monkeypatch.setattr(mod.workspace_roots, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(mod.workspace_roots, "CRYPTO_HISTORY_ROOT", history_dir)

    assert mod._preferred_basis_spot_csv("BTC") == dashboard


def test_supervisor_peer_returns_supply_30_return_window(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="btc_primary",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    peer = BotInstance(
        bot_id="eth_peer",
        symbol="ETH",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    for idx in range(31):
        peer.sage_bars.append({"close": 100.0 + idx, "ts": f"p{idx}"})
    sup.bots.extend([bot, peer])

    peer_returns = sup._peer_returns_for_bot(bot)

    assert "ETH" in peer_returns
    assert len(peer_returns["ETH"]) == 30


def _reset_live_ibkr(mod) -> None:
    """Stop the dedicated thread + null the globals so each test starts clean."""
    if mod._LIVE_IBKR_LOOP is not None and not mod._LIVE_IBKR_LOOP.is_closed():
        mod._LIVE_IBKR_LOOP.call_soon_threadsafe(mod._LIVE_IBKR_LOOP.stop)
    if mod._LIVE_IBKR_THREAD is not None and mod._LIVE_IBKR_THREAD.is_alive():
        mod._LIVE_IBKR_THREAD.join(timeout=2.0)
    mod._LIVE_IBKR_LOOP = None
    mod._LIVE_IBKR_THREAD = None


def test_live_ibkr_loop_helper_returns_same_loop_across_calls() -> None:
    """Regression: each call MUST return the same persistent loop.

    Earlier the supervisor created a fresh event loop per direct order
    and immediately closed it. ib_insync/eventkit caches loop bindings
    on its _OverlappedFuture objects, so any fresh-loop-per-call pattern
    raised "Future attached to a different loop" on Windows.
    """
    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    _reset_live_ibkr(mod)
    try:
        loop_a = mod._get_or_create_live_ibkr_loop()
        loop_b = mod._get_or_create_live_ibkr_loop()
        assert loop_a is loop_b
        assert not loop_a.is_closed()
        assert mod._LIVE_IBKR_THREAD is not None
        assert mod._LIVE_IBKR_THREAD.is_alive()
    finally:
        _reset_live_ibkr(mod)


def test_run_on_live_ibkr_loop_executes_coroutine() -> None:
    """The dispatcher schedules a coroutine onto the persistent loop and returns the result."""
    import asyncio

    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    _reset_live_ibkr(mod)
    try:

        async def _echo(x):
            await asyncio.sleep(0)
            return x * 2

        result = mod._run_on_live_ibkr_loop(_echo(21), timeout=5.0)
        assert result == 42
    finally:
        _reset_live_ibkr(mod)


def test_run_on_live_ibkr_loop_runs_in_dedicated_thread() -> None:
    """All scheduled coroutines must execute on the dedicated thread, not the caller's."""
    import threading

    from eta_engine.scripts import jarvis_strategy_supervisor as mod

    _reset_live_ibkr(mod)
    try:
        caller_tid = threading.get_ident()

        async def _capture_thread():
            return threading.get_ident()

        runner_tid = mod._run_on_live_ibkr_loop(_capture_thread(), timeout=5.0)
        assert runner_tid != caller_tid
        assert runner_tid == mod._LIVE_IBKR_THREAD.ident
    finally:
        _reset_live_ibkr(mod)

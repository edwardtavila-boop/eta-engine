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


def test_env_file_loader_tolerates_non_utf8_bytes(tmp_path: Path) -> None:
    from eta_engine.scripts.jarvis_strategy_supervisor import _read_env_file_lines

    env_path = tmp_path / ".env"
    env_path.write_bytes(b"ETA_SUPERVISOR_FEED=composite\n# bad byte: \x9d\n")

    assert _read_env_file_lines(env_path)[0] == "ETA_SUPERVISOR_FEED=composite"


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
        bot_id="paperlive", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bar = {"close": 100.0}
    router.submit_entry(
        bot=bot, signal_id="s1", side="BUY", bar=bar, size_mult=1.0,
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
        bot_id="paperlive", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bar = {"close": 100.0}
    router.submit_entry(
        bot=bot, signal_id="s1", side="BUY", bar=bar, size_mult=1.0,
    )
    pending = tmp_path / "paperlive.pending_order.json"
    assert pending.exists()


def test_router_paper_live_broker_router_bypasses_allowed_symbols(
    tmp_path: Path, monkeypatch,
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
        bot_id="crypto_live_paused", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )

    rec = router.submit_entry(
        bot=bot, signal_id="s1", side="BUY", bar={"close": 100.0}, size_mult=1.0,
    )

    # broker_router route writes the pending_order JSON regardless of
    # the legacy allowlist; routing yaml decides where it actually goes.
    assert rec is not None
    assert (tmp_path / "crypto_live_paused.pending_order.json").exists()


def test_router_paper_live_direct_ibkr_still_honors_allowed_symbols(
    tmp_path: Path, monkeypatch,
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
        bot_id="crypto_live_paused", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )

    rec = router.submit_entry(
        bot=bot, signal_id="s1", side="BUY", bar={"close": 100.0}, size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None


def test_router_paper_live_order_entry_hold_blocks_before_position(
    tmp_path: Path, monkeypatch,
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
        bot_id="held", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )

    rec = router.submit_entry(
        bot=bot, signal_id="s-held", side="BUY",
        bar={"close": 100.0}, size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None
    assert bot.n_entries == 0
    assert not (tmp_path / "held.pending_order.json").exists()


def test_router_paper_live_direct_reject_rolls_back_position(
    tmp_path: Path, monkeypatch,
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
    tmp_path: Path, monkeypatch,
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


def test_router_paper_live_futures_floor_reaches_broker_with_small_cash(
    tmp_path: Path, monkeypatch,
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
        bot_id="bracket_bot", symbol="BTC", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bar = {"close": 100.0, "high": 101.0, "low": 99.0, "open": 99.5}
    router.submit_entry(
        bot=bot, signal_id="bracket-1", side="BUY", bar=bar, size_mult=1.0,
    )
    pending = tmp_path / "bracket_bot.pending_order.json"
    assert pending.exists()
    payload = json.loads(pending.read_text(encoding="utf-8"))
    # Schema must now carry the bracket fields.
    assert "stop_price" in payload
    assert "target_price" in payload
    # When the supervisor's bracket-compute path succeeded these mirror
    # bot.open_position; if it fell through to the warn-once branch the
    # writer still emits the keys (with null values) so the venue can
    # fail-closed downstream rather than the supervisor dropping them.
    pos = bot.open_position or {}
    assert payload["stop_price"] == pos.get("bracket_stop")
    assert payload["target_price"] == pos.get("bracket_target")


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
              "ETA_SUPERVISOR_MODE", "ETA_SUPERVISOR_STATE_DIR",
              "ETA_LIVE_MONEY"):
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

    assert cfg.state_dir == (
        supervisor.workspace_roots.ETA_RUNTIME_STATE_DIR
        / "jarvis_intel"
        / "supervisor_mock"
    )


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
        sup.bots.append(BotInstance(
            bot_id=bot_id, symbol="BTC", strategy_kind="x",
            direction="long", cash=5000.0,
        ))
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
            f"bot {bot['bot_id']} reports mode={bot.get('mode')!r}, "
            "expected paper_live (cfg.mode inheritance broken)"
        )


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


# ─── LiveIbkrVenue dedicated-loop-thread dispatcher ───────────────


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

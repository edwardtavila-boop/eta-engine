"""Tests for scripts.run_apex_live — the tie-together runtime loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from apex_predator.core.broker_equity_poller import BrokerEquityPoller
from apex_predator.core.broker_equity_reconciler import BrokerEquityReconciler
from apex_predator.core.consistency_guard import (
    ConsistencyGuard,
    ConsistencyStatus,
)
from apex_predator.core.kill_switch_latch import KillSwitchLatch, LatchState
from apex_predator.core.kill_switch_runtime import (
    BotSnapshot,
    KillAction,
    KillSeverity,
    KillVerdict,
)
from apex_predator.core.trailing_dd_tracker import TrailingDDTracker
from apex_predator.scripts import run_apex_live as mod


# --------------------------------------------------------------------------- #
# Isolation: every ApexRuntime constructed in this module auto-builds a
# disk-backed KillSwitchLatch under ROOT/"state". If we let that default
# through, one test's FLATTEN_ALL would leave a TRIPPED latch on the repo
# disk that blocks every subsequent run. Redirect ROOT to a tmp path for
# the duration of each test.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_runtime_state(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(mod, "ROOT", tmp_path)


# --------------------------------------------------------------------------- #
# Fake bot — replaces real MnqBot/NqBot/etc. in the binding table so tests
# never touch heavy imports or try to call Tradovate.
# --------------------------------------------------------------------------- #
@dataclass
class _FakeState:
    equity: float = 5000.0
    peak_equity: float = 5000.0
    todays_pnl: float = 0.0
    consecutive_losses: int = 0
    open_positions: list = None  # type: ignore[assignment]
    is_killed: bool = False
    is_paused: bool = False

    def __post_init__(self) -> None:
        if self.open_positions is None:
            self.open_positions = []


@dataclass
class _FakeConfig:
    name: str
    symbol: str
    risk_per_trade_pct: float = 1.0


class FakeBot:
    def __init__(self, name: str, symbol: str, tier: str) -> None:
        self.config = _FakeConfig(name=name, symbol=symbol)
        self.state = _FakeState()
        self.bars_seen: list[dict] = []
        self.started = False
        self.stopped = False
        self.tier = tier
        self.runtime_snapshot: dict[str, Any] = {}

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def on_bar(self, bar: dict) -> None:
        self.bars_seen.append(bar)


def _fake_bindings() -> list[mod.BotBinding]:
    """Rebind all six slots to FakeBot so we never import real bot code."""
    def mk(name, symbol, tier):
        return lambda: FakeBot(name, symbol, tier)
    return [
        mod.BotBinding("mnq",         "A", "tier_a_mnq_live",  mk("mnq", "MNQ", "A"),         "MNQ"),
        mod.BotBinding("nq",          "A", "tier_a_nq_live",   mk("nq",  "NQ",  "A"),         "NQ"),
        mod.BotBinding("crypto_seed", "B", "tier_b_testnet",   mk("crypto_seed", "BTCUSDT", "B"), "BTCUSDT"),
        mod.BotBinding("eth_perp",    "B", "tier_b_mainnet",   mk("eth_perp", "ETHUSDT", "B"),   "ETHUSDT"),
        mod.BotBinding("sol_perp",    "B", "tier_b_mainnet",   mk("sol_perp", "SOLUSDT", "B"),   "SOLUSDT"),
        mod.BotBinding("xrp_perp",    "B", "tier_b_mainnet",   mk("xrp_perp", "XRPUSDT", "B"),   "XRPUSDT"),
    ]


def _cfg_factory(tmp_path: Path, **overrides) -> mod.RuntimeConfig:
    cfg = mod.RuntimeConfig(
        tradovate={"apex_eval": {"trailing_drawdown_usd": 2500.0}},
        bybit={},
        alerts={
            "rate_limit": {"info_per_minute": 1000, "warn_per_minute": 1000, "critical_per_minute": 0},
            "channels": {},
            "routing": {"events": {
                "runtime_start": {"level": "info", "channels": []},
                "runtime_stop":  {"level": "info", "channels": []},
                "kill_switch":   {"level": "critical", "channels": []},
                "circuit_trip":  {"level": "warn", "channels": []},
                "apex_preempt":  {"level": "critical", "channels": []},
                "bot_error":     {"level": "warn", "channels": []},
                "bot_entry":     {"level": "info", "channels": []},
            }},
        },
        kill_switch={
            "global": {"max_drawdown_kill_pct_of_portfolio": 100.0, "daily_loss_cap_pct_of_portfolio": 100.0},
            "tier_a": {"per_bucket": {}, "apex_eval_preemptive": {"cushion_usd": 0}},
            "tier_b": {"per_bucket": {}, "correlation_kill": {"enabled": False},
                       "funding_veto": {"soft_threshold_bps": 20, "hard_threshold_bps": 50}},
        },
        go_state={},
        live=False,
        dry_run=True,
        tick_interval_s=0.0,
        max_bars=2,
        state_path=tmp_path / "state.json",
        config_dir=tmp_path / "configs",
        log_path=tmp_path / "runtime.jsonl",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- #
# select_active_bots
# --------------------------------------------------------------------------- #
def test_select_active_bots_honors_flags():
    bindings = _fake_bindings()
    state = {"tier_a_mnq_live": True, "tier_b_mainnet": True}
    got = mod.select_active_bots(bindings, state, bot_filter=None)
    names = [b.name for b in got]
    assert "mnq" in names
    assert "eth_perp" in names
    assert "sol_perp" in names
    assert "xrp_perp" in names
    assert "nq" not in names
    assert "crypto_seed" not in names


def test_select_active_bots_honors_bot_filter():
    bindings = _fake_bindings()
    state = {"tier_a_mnq_live": True, "tier_a_nq_live": True}
    got = mod.select_active_bots(bindings, state, bot_filter="mnq")
    assert [b.name for b in got] == ["mnq"]


def test_select_active_bots_returns_empty_on_kill_switch():
    bindings = _fake_bindings()
    state = {"tier_a_mnq_live": True, "kill_switch_active": True}
    got = mod.select_active_bots(bindings, state, bot_filter=None)
    assert got == []


# --------------------------------------------------------------------------- #
# Snapshot builders
# --------------------------------------------------------------------------- #
def test_build_bot_snapshot_from_fake():
    binding = mod.BotBinding("mnq", "A", "tier_a_mnq_live", lambda: None, "MNQ")
    bot = FakeBot("mnq", "MNQ", "A")
    bot.state.equity = 4800
    bot.state.peak_equity = 5000
    bot.state.todays_pnl = -200
    bot.state.consecutive_losses = 2
    bot.runtime_snapshot = {
        "market_context_summary": {
            "market_context_regime": "RISK_ON",
            "market_context_external_score": 9.1,
            "session_timeframe_key": "OPEN_DRIVE::M15",
            "spread_regime": "TIGHT",
            "order_book_quality": 8.5,
        }
    }
    snap = mod.build_bot_snapshot(binding, bot)
    assert snap.name == "mnq"
    assert snap.tier == "A"
    assert snap.equity_usd == 4800
    assert snap.peak_equity_usd == 5000
    assert snap.session_realized_pnl_usd == -200
    assert snap.consecutive_losses == 2
    assert snap.market_context_summary is not None
    assert snap.market_context_summary["session_timeframe_key"] == "OPEN_DRIVE::M15"
    assert snap.market_context_summary_text.startswith(
        "market_context=RISK_ON quality=0.00 tf=OPEN_DRIVE::M15 spread=TIGHT",
    )
    assert "ext=9.10" in snap.market_context_summary_text
    assert "obq=8.50" in snap.market_context_summary_text


def test_build_portfolio_snapshot_aggregates():
    snaps = [
        BotSnapshot(name="a", tier="A", equity_usd=5000, peak_equity_usd=5000, session_realized_pnl_usd=-100),
        BotSnapshot(name="b", tier="B", equity_usd=2000, peak_equity_usd=2000, session_realized_pnl_usd=-50),
    ]
    p = mod.build_portfolio_snapshot(snaps)
    assert p.total_equity_usd == 7000
    assert p.peak_equity_usd == 7000
    assert p.daily_realized_pnl_usd == -150


def test_build_apex_eval_snapshot_computes_distance(tmp_path):
    cfg = _cfg_factory(tmp_path)
    snaps = [BotSnapshot(name="mnq", tier="A", equity_usd=4800, peak_equity_usd=5000)]
    ae = mod.build_apex_eval_snapshot(cfg, snaps)
    assert ae.trailing_dd_limit_usd == 2500.0
    assert ae.distance_to_limit_usd == pytest.approx(2300.0)


def test_build_apex_eval_snapshot_empty_tier_a(tmp_path):
    cfg = _cfg_factory(tmp_path)
    ae = mod.build_apex_eval_snapshot(cfg, [])
    # Defaults — full cushion
    assert ae.distance_to_limit_usd == 2500.0


# --------------------------------------------------------------------------- #
# apply_verdict — every KillAction path
# --------------------------------------------------------------------------- #
class _StubDispatcher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def send(self, event: str, payload: dict):
        self.sent.append((event, payload))
        return None


class _StubRouter:
    name = "stub"

    def __init__(self) -> None:
        self.flattened: list[tuple[str, str]] = []

    async def flatten(self, symbol: str, reason: str):
        self.flattened.append((symbol, reason))


@pytest.mark.asyncio
async def test_apply_verdict_continue_is_noop():
    disp = _StubDispatcher()
    router = _StubRouter()
    v = KillVerdict(action=KillAction.CONTINUE, severity=KillSeverity.INFO, reason="ok", scope="global")
    rep = await mod.apply_verdict(v, [], router, disp)
    assert rep.executed == []
    assert disp.sent == []


@pytest.mark.asyncio
async def test_apply_verdict_flatten_bot_scoped_by_name():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    mnq = (bindings[0], FakeBot("mnq", "MNQ", "A"))
    v = KillVerdict(action=KillAction.FLATTEN_BOT, severity=KillSeverity.WARN,
                    reason="test", scope="bot:mnq")
    await mod.apply_verdict(v, [mnq], router, disp)
    assert mnq[1].state.is_paused is True
    assert router.flattened == [("MNQ", "test")]
    assert any(e[0] == "kill_switch" for e in disp.sent)


@pytest.mark.asyncio
async def test_apply_verdict_flatten_bot_scoped_by_symbol():
    """Funding emits scope=bot:<symbol>; make sure we match that too."""
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    eth = (bindings[3], FakeBot("eth_perp", "ETHUSDT", "B"))
    v = KillVerdict(action=KillAction.FLATTEN_BOT, severity=KillSeverity.WARN,
                    reason="funding", scope="bot:ETHUSDT")
    await mod.apply_verdict(v, [eth], router, disp)
    assert router.flattened == [("ETHUSDT", "funding")]


@pytest.mark.asyncio
async def test_apply_verdict_flatten_all_flags_all_bots():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    mnq = (bindings[0], FakeBot("mnq", "MNQ", "A"))
    eth = (bindings[3], FakeBot("eth_perp", "ETHUSDT", "B"))
    v = KillVerdict(action=KillAction.FLATTEN_ALL, severity=KillSeverity.CRITICAL,
                    reason="port DD", scope="global")
    await mod.apply_verdict(v, [mnq, eth], router, disp)
    assert mnq[1].state.is_killed is True
    assert eth[1].state.is_killed is True
    assert len(router.flattened) == 2


@pytest.mark.asyncio
async def test_apply_verdict_flatten_tier_b_only():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    mnq = (bindings[0], FakeBot("mnq", "MNQ", "A"))
    eth = (bindings[3], FakeBot("eth_perp", "ETHUSDT", "B"))
    v = KillVerdict(action=KillAction.FLATTEN_TIER_B, severity=KillSeverity.WARN,
                    reason="corr", scope="tier_b")
    await mod.apply_verdict(v, [mnq, eth], router, disp)
    assert mnq[1].state.is_paused is False  # tier-A untouched
    assert eth[1].state.is_paused is True
    assert router.flattened == [("ETHUSDT", "corr")]


@pytest.mark.asyncio
async def test_apply_verdict_flatten_tier_a_preempt():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    mnq = (bindings[0], FakeBot("mnq", "MNQ", "A"))
    nq = (bindings[1], FakeBot("nq", "NQ", "A"))
    eth = (bindings[3], FakeBot("eth_perp", "ETHUSDT", "B"))
    v = KillVerdict(action=KillAction.FLATTEN_TIER_A_PREEMPTIVE, severity=KillSeverity.CRITICAL,
                    reason="apex cushion", scope="tier_a")
    await mod.apply_verdict(v, [mnq, nq, eth], router, disp)
    assert mnq[1].state.is_paused is True
    assert nq[1].state.is_paused is True
    assert eth[1].state.is_paused is False
    assert any(e[0] == "apex_preempt" for e in disp.sent)


@pytest.mark.asyncio
async def test_apply_verdict_halve_size_cuts_risk_pct():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    eth = (bindings[3], FakeBot("eth_perp", "ETHUSDT", "B"))
    eth[1].config.risk_per_trade_pct = 2.0
    v = KillVerdict(action=KillAction.HALVE_SIZE, severity=KillSeverity.INFO,
                    reason="funding soft", scope="bot:ETHUSDT",
                    evidence={"symbol": "ETHUSDT"})
    await mod.apply_verdict(v, [eth], router, disp)
    assert eth[1].config.risk_per_trade_pct == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_apply_verdict_pause_new_entries():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    mnq = (bindings[0], FakeBot("mnq", "MNQ", "A"))
    v = KillVerdict(action=KillAction.PAUSE_NEW_ENTRIES, severity=KillSeverity.WARN,
                    reason="warn", scope="bot:mnq")
    await mod.apply_verdict(v, [mnq], router, disp)
    assert mnq[1].state.is_paused is True


# --------------------------------------------------------------------------- #
# ApexRuntime end-to-end
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_runtime_runs_zero_bars_when_no_bots_active(tmp_path):
    cfg = _cfg_factory(tmp_path, go_state={})
    runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
    rc = await runtime.run()
    assert rc == 0
    lines = (tmp_path / "runtime.jsonl").read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(ln)["kind"] for ln in lines]
    assert "no_active_bots" in kinds


@pytest.mark.asyncio
async def test_runtime_ticks_active_bot(tmp_path):
    cfg = _cfg_factory(tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3)
    runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
    rc = await runtime.run()
    assert rc == 0
    lines = (tmp_path / "runtime.jsonl").read_text(encoding="utf-8").strip().splitlines()
    tick_entries = [json.loads(ln) for ln in lines if json.loads(ln)["kind"] == "tick"]
    assert len(tick_entries) == 3
    for e in tick_entries:
        assert "mnq" in e["active"]


@pytest.mark.asyncio
async def test_runtime_honors_operator_kill_between_ticks(tmp_path):
    """Pre-arm the runtime with an active bot, then stamp the kill flag to
    disk before run() starts — the first tick's _refresh_go_state() must pick
    it up and short-circuit to operator_kill."""
    cfg = _cfg_factory(tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=10)
    runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
    # MNQ bot is instantiated during run() from cfg.go_state (still active).
    # Now stamp the kill so the tick-start refresh flips the live flag.
    cfg.state_path.write_text(json.dumps({
        "shared_artifacts": {"apex_go_state": {
            "tier_a_mnq_live": True,
            "kill_switch_active": True,
        }}
    }))
    rc = await runtime.run()
    assert rc == 0
    lines = (tmp_path / "runtime.jsonl").read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(ln)["kind"] for ln in lines]
    assert "operator_kill" in kinds
    # We should stop well before max_bars=10
    ticks = kinds.count("tick")
    assert ticks < 10


@pytest.mark.asyncio
async def test_runtime_flatten_all_stops_loop(tmp_path):
    """Force an immediate FLATTEN_ALL by configuring an impossible DD cap."""
    cfg = _cfg_factory(tmp_path,
                       go_state={"tier_a_mnq_live": True}, max_bars=10)
    cfg.kill_switch = {
        "global": {
            "max_drawdown_kill_pct_of_portfolio": 0.01,  # any loss trips
            "daily_loss_cap_pct_of_portfolio": 100.0,
        },
        "tier_a": {"per_bucket": {}, "apex_eval_preemptive": {"cushion_usd": 0}},
        "tier_b": {"per_bucket": {}, "correlation_kill": {"enabled": False},
                   "funding_veto": {"soft_threshold_bps": 20, "hard_threshold_bps": 50}},
    }
    # Force a loss so DD > 0
    bindings = _fake_bindings()
    def _mk_losing_mnq():
        b = FakeBot("mnq", "MNQ", "A")
        b.state.equity = 4000
        b.state.peak_equity = 5000
        return b
    bindings[0] = mod.BotBinding("mnq", "A", "tier_a_mnq_live", _mk_losing_mnq, "MNQ")
    runtime = mod.ApexRuntime(cfg, bindings=bindings,
                              kill_switch=mod.KillSwitch(cfg.kill_switch))
    rc = await runtime.run()
    assert rc == 0
    lines = (tmp_path / "runtime.jsonl").read_text(encoding="utf-8").strip().splitlines()
    verdict_actions: list[str] = []
    for ln in lines:
        e = json.loads(ln)
        if e["kind"] == "tick":
            for v in e["verdicts"]:
                verdict_actions.append(v["action"])
    assert "FLATTEN_ALL" in verdict_actions


@pytest.mark.asyncio
async def test_runtime_uses_mock_router_in_dry_run(tmp_path):
    cfg = _cfg_factory(tmp_path, go_state={"tier_a_mnq_live": True})
    runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
    assert isinstance(runtime.router, mod.MockRouter)


# --------------------------------------------------------------------------- #
# Config loader
# --------------------------------------------------------------------------- #
def test_load_runtime_config_reads_all_yamls(tmp_path: Path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "tradovate.yaml").write_text("venue: tradovate\n", encoding="utf-8")
    (cfg_dir / "bybit.yaml").write_text("venue: bybit\n", encoding="utf-8")
    (cfg_dir / "alerts.yaml").write_text("channels: {}\n", encoding="utf-8")
    (cfg_dir / "kill_switch.yaml").write_text("global: {}\n", encoding="utf-8")
    state = tmp_path / "roadmap_state.json"
    state.write_text(json.dumps({"shared_artifacts": {"apex_go_state": {"tier_a_mnq_live": True}}}))
    cfg = mod.load_runtime_config(
        config_dir=cfg_dir, state_path=state,
        live=False, dry_run=True, bot_filter=None,
        max_bars=1, tick_interval_s=0.0,
        log_path=tmp_path / "x.jsonl",
    )
    assert cfg.tradovate == {"venue": "tradovate"}
    assert cfg.bybit == {"venue": "bybit"}
    assert cfg.alerts == {"channels": {}}
    assert cfg.kill_switch == {"global": {}}
    assert cfg.go_state == {"tier_a_mnq_live": True}


# --------------------------------------------------------------------------- #
# R2 closure: tick-cadence validation in live mode
# --------------------------------------------------------------------------- #
class TestLoadRuntimeConfigTickCadence:

    def _write_yamls(self, cfg_dir: Path, cushion_usd: float = 500.0) -> None:
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "tradovate.yaml").write_text(
            "apex_eval:\n  trailing_drawdown_usd: 2500.0\n", encoding="utf-8",
        )
        (cfg_dir / "bybit.yaml").write_text("{}\n", encoding="utf-8")
        (cfg_dir / "alerts.yaml").write_text("channels: {}\n", encoding="utf-8")
        (cfg_dir / "kill_switch.yaml").write_text(
            "global: {}\n"
            "tier_a:\n"
            f"  apex_eval_preemptive:\n    cushion_usd: {cushion_usd}\n",
            encoding="utf-8",
        )

    def test_live_mode_fast_tick_passes(self, tmp_path: Path):
        cfg_dir = tmp_path / "configs"
        self._write_yamls(cfg_dir, cushion_usd=1000.0)
        state = tmp_path / "state.json"
        state.write_text("{}")
        cfg = mod.load_runtime_config(
            config_dir=cfg_dir, state_path=state,
            live=True, dry_run=False, bot_filter=None,
            max_bars=1, tick_interval_s=1.0,
            log_path=tmp_path / "x.jsonl",
        )
        assert cfg.live is True
        assert cfg.tick_interval_s == 1.0

    def test_live_mode_slow_tick_raises(self, tmp_path: Path):
        cfg_dir = tmp_path / "configs"
        self._write_yamls(cfg_dir, cushion_usd=500.0)
        state = tmp_path / "state.json"
        state.write_text("{}")
        # 5s tick + 500 cushion + default 300 usd/sec * 2 safety = 3000 > 500
        from apex_predator.core.kill_switch_runtime import ApexTickCadenceError
        with pytest.raises(ApexTickCadenceError, match="tick cadence"):
            mod.load_runtime_config(
                config_dir=cfg_dir, state_path=state,
                live=True, dry_run=False, bot_filter=None,
                max_bars=1, tick_interval_s=5.0,
                log_path=tmp_path / "x.jsonl",
            )

    def test_paper_mode_slow_tick_allowed(self, tmp_path: Path):
        cfg_dir = tmp_path / "configs"
        self._write_yamls(cfg_dir, cushion_usd=500.0)
        state = tmp_path / "state.json"
        state.write_text("{}")
        # live=False => validator no-ops even with clearly-unsafe cadence
        cfg = mod.load_runtime_config(
            config_dir=cfg_dir, state_path=state,
            live=False, dry_run=True, bot_filter=None,
            max_bars=1, tick_interval_s=60.0,
            log_path=tmp_path / "x.jsonl",
        )
        assert cfg.live is False

    def test_default_tick_interval_is_one_second(self):
        """R2: the class-level default moved from 5.0 -> 1.0 post-R2."""
        cfg = mod.RuntimeConfig()
        assert cfg.tick_interval_s == 1.0


# --------------------------------------------------------------------------- #
# Router selection: real vs mock
#
# Per the 2026-04-24 broker-dormancy mandate, the active-broker check
# looks at IBKR (primary) + Tastytrade (fallback). Tradovate creds are
# explicitly NOT enough to flip --live into the real-router branch
# because Tradovate is dormant in venues.router.DORMANT_BROKERS.
# --------------------------------------------------------------------------- #
def test_active_broker_creds_absent_by_default(monkeypatch):
    """No env -> False (conservative -- don't auto-spin a real venue)."""
    monkeypatch.setattr(
        "apex_predator.venues.ibkr.IbkrClientPortalVenue.has_credentials",
        lambda self: False,
    )
    monkeypatch.setattr(
        "apex_predator.venues.tastytrade.TastytradeVenue.has_credentials",
        lambda self: False,
    )
    assert mod._active_broker_creds_present() is False


def test_active_broker_creds_present_via_ibkr(monkeypatch):
    """IBKR present -> True regardless of Tasty."""
    monkeypatch.setattr(
        "apex_predator.venues.ibkr.IbkrClientPortalVenue.has_credentials",
        lambda self: True,
    )
    assert mod._active_broker_creds_present() is True


def test_active_broker_creds_present_via_tastytrade(monkeypatch):
    """Tasty present (IBKR absent) -> True via fallback."""
    monkeypatch.setattr(
        "apex_predator.venues.ibkr.IbkrClientPortalVenue.has_credentials",
        lambda self: False,
    )
    monkeypatch.setattr(
        "apex_predator.venues.tastytrade.TastytradeVenue.has_credentials",
        lambda self: True,
    )
    assert mod._active_broker_creds_present() is True


def test_tradovate_creds_alone_do_not_flip_check(monkeypatch):
    """The dormant broker is not an 'active' broker. Tradovate creds
    set, IBKR + Tasty empty -> the active-broker check returns False."""
    monkeypatch.setattr(
        "apex_predator.venues.ibkr.IbkrClientPortalVenue.has_credentials",
        lambda self: False,
    )
    monkeypatch.setattr(
        "apex_predator.venues.tastytrade.TastytradeVenue.has_credentials",
        lambda self: False,
    )
    monkeypatch.setenv("TRADOVATE_CLIENT_ID", "x")
    monkeypatch.setenv("TRADOVATE_CLIENT_SECRET", "x")
    monkeypatch.setenv("TRADOVATE_USERNAME", "x")
    monkeypatch.setenv("TRADOVATE_PASSWORD", "x")
    assert mod._active_broker_creds_present() is False


def test_tradovate_creds_present_alias_still_resolves(monkeypatch):
    """Backward-compat shim -- existing callers can still import the
    old name without breaking. New code should use the new name."""
    assert mod._tradovate_creds_present is mod._active_broker_creds_present


# --------------------------------------------------------------------------- #
# CLI parse
# --------------------------------------------------------------------------- #
def test_parse_args_defaults_to_dry_run():
    args = mod.parse_args([])
    assert args.dry_run is True
    assert args.live is False
    assert args.max_bars == 0


def test_parse_args_live_flag():
    args = mod.parse_args(["--live", "--max-bars", "5", "--bot", "mnq"])
    assert args.live is True
    assert args.max_bars == 5
    assert args.bot == "mnq"


# --------------------------------------------------------------------------- #
# D5 -- KillSwitchLatch integration (end-to-end)
#
# The latch is the boot gate + verdict durability layer on top of the
# stateless KillSwitch. These tests cover:
#   * TRIPPED latch refuses runtime.run() with exit code 3, never calls
#     start() on any bot, and never hits the router
#   * ARMED latch permits normal boot
#   * FLATTEN_ALL verdict flips the latch to TRIPPED (survives restart)
#   * HALVE_SIZE (non-latching) does NOT flip the latch
# --------------------------------------------------------------------------- #
class TestLatchIntegration:
    @pytest.mark.asyncio
    async def test_runtime_refuses_boot_when_latch_tripped(self, tmp_path):
        """Pre-trip a latch on disk then boot -- must return 3 immediately."""
        latch_path = tmp_path / "state" / "kill_switch_latch.json"
        latch = KillSwitchLatch(latch_path)
        latch.record_verdict(KillVerdict(
            action=KillAction.FLATTEN_ALL,
            severity=KillSeverity.CRITICAL,
            reason="daily loss 6.02% >= cap 6%",
            scope="global",
        ))
        assert latch.read().state is LatchState.TRIPPED

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        bindings = _fake_bindings()
        runtime = mod.ApexRuntime(
            cfg, bindings=bindings,
            kill_switch_latch=KillSwitchLatch(latch_path),
        )
        rc = await runtime.run()
        assert rc == 3

        # The boot_refused event must be in the log. No tick entries
        # should exist -- we never got past the boot gate.
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        kinds = [json.loads(line)["kind"] for line in lines]
        assert "boot_refused" in kinds
        assert "tick" not in kinds
        assert "runtime_start" not in kinds

    @pytest.mark.asyncio
    async def test_runtime_boots_when_latch_armed(self, tmp_path):
        """Fresh latch = ARMED = boot proceeds normally."""
        latch_path = tmp_path / "state" / "kill_switch_latch.json"
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            kill_switch_latch=KillSwitchLatch(latch_path),
        )
        rc = await runtime.run()
        assert rc == 0
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        kinds = [json.loads(line)["kind"] for line in lines]
        assert "runtime_start" in kinds
        assert kinds.count("tick") == 2
        assert "boot_refused" not in kinds

    @pytest.mark.asyncio
    async def test_flatten_all_verdict_persists_to_latch(self, tmp_path):
        """A FLATTEN_ALL verdict during run() must flip the latch to TRIPPED."""
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=10,
        )
        cfg.kill_switch = {
            "global": {
                "max_drawdown_kill_pct_of_portfolio": 0.01,  # trip on any loss
                "daily_loss_cap_pct_of_portfolio": 100.0,
            },
            "tier_a": {
                "per_bucket": {},
                "apex_eval_preemptive": {"cushion_usd": 0},
            },
            "tier_b": {
                "per_bucket": {},
                "correlation_kill": {"enabled": False},
                "funding_veto": {"soft_threshold_bps": 20, "hard_threshold_bps": 50},
            },
        }
        bindings = _fake_bindings()

        def _losing_mnq() -> FakeBot:
            b = FakeBot("mnq", "MNQ", "A")
            b.state.equity = 4000
            b.state.peak_equity = 5000
            return b

        bindings[0] = mod.BotBinding(
            "mnq", "A", "tier_a_mnq_live", _losing_mnq, "MNQ",
        )
        latch_path = tmp_path / "state" / "kill_switch_latch.json"
        latch = KillSwitchLatch(latch_path)
        assert latch.read().state is LatchState.ARMED

        runtime = mod.ApexRuntime(
            cfg, bindings=bindings,
            kill_switch=mod.KillSwitch(cfg.kill_switch),
            kill_switch_latch=latch,
        )
        await runtime.run()

        # After the run, the latch must be TRIPPED on disk.
        persisted = KillSwitchLatch(latch_path).read()
        assert persisted.state is LatchState.TRIPPED
        assert persisted.action == KillAction.FLATTEN_ALL.value

        # And a subsequent runtime with this path refuses to boot.
        cfg2 = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
            log_path=tmp_path / "runtime2.jsonl",
        )
        runtime2 = mod.ApexRuntime(
            cfg2, bindings=_fake_bindings(),
            kill_switch_latch=KillSwitchLatch(latch_path),
        )
        rc2 = await runtime2.run()
        assert rc2 == 3

    @pytest.mark.asyncio
    async def test_halve_size_verdict_does_not_trip_latch(self, tmp_path):
        """Non-latching verdicts (HALVE_SIZE) must leave the latch ARMED.

        We test this at the record_verdict boundary rather than forcing a
        HALVE_SIZE through the whole pipeline -- the runtime forwards
        every verdict to the latch and relies on record_verdict's own
        _LATCHING_ACTIONS filter.
        """
        latch_path = tmp_path / "state" / "kill_switch_latch.json"
        latch = KillSwitchLatch(latch_path)
        halve = KillVerdict(
            action=KillAction.HALVE_SIZE, severity=KillSeverity.INFO,
            reason="funding soft", scope="bot:ETHUSDT",
        )
        changed = latch.record_verdict(halve)
        assert changed is False
        assert latch.read().state is LatchState.ARMED

    @pytest.mark.asyncio
    async def test_runtime_auto_constructs_latch_when_absent(self, tmp_path):
        """Default path: no kill_switch_latch kwarg -> auto-construct under ROOT/state.
        With the autouse fixture ROOT = tmp_path, so the latch lives there.
        """
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        # The default path is ROOT/"state"/"kill_switch_latch.json"
        expected = tmp_path / "state" / "kill_switch_latch.json"
        assert runtime.kill_switch_latch.path == expected
        rc = await runtime.run()
        assert rc == 0


# --------------------------------------------------------------------------- #
# D2 -- TrailingDDTracker integration (tick-granular apex_eval path)
#
# When a tracker is attached, build_apex_eval_snapshot() is bypassed and the
# tracker becomes the source of truth for ApexEvalSnapshot. These tests cover:
#   * no tracker attached = legacy path is used (smoke test, shape only)
#   * tracker attached = tracker.update() is called per loop and its peak
#     monotonically moves up
#   * tracker's near-breach cushion triggers FLATTEN_TIER_A_PREEMPTIVE +
#     latch TRIPPED
#   * tracker state survives restart (peak not reset on second runtime)
# --------------------------------------------------------------------------- #
class TestTrailingDDTrackerIntegration:
    @pytest.mark.asyncio
    async def test_no_tracker_uses_legacy_apex_eval_path(self, tmp_path):
        """Without a tracker, build_apex_eval_snapshot is used (legacy shape)."""
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert runtime.trailing_dd_tracker is None
        rc = await runtime.run()
        assert rc == 0

    @pytest.mark.asyncio
    async def test_tracker_attached_receives_equity_updates(self, tmp_path):
        """Tracker.update() fires every loop with tier-A aggregate equity."""
        tracker_path = tmp_path / "state" / "trailing_dd.json"
        tracker = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=5_000.0,  # matches FakeBot default equity
            trailing_dd_cap_usd=500.0,
        )
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            trailing_dd_tracker=tracker,
        )
        rc = await runtime.run()
        assert rc == 0
        # Tracker received at least one update -- last_equity_usd is set.
        assert tracker.state().last_equity_usd == 5_000.0
        # Peak did not exceed baseline because fake equity is flat.
        assert tracker.state().peak_equity_usd == 5_000.0

    @pytest.mark.asyncio
    async def test_tracker_breach_triggers_preemptive_flatten_and_latches(
        self, tmp_path,
    ):
        """Tracker floor breach -> KillSwitch fires FLATTEN_TIER_A_PREEMPTIVE
        -> latch TRIPPED -> next runtime refuses to boot."""
        tracker_path = tmp_path / "state" / "trailing_dd.json"
        tracker = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=5_000.0,
            trailing_dd_cap_usd=500.0,
        )
        # Pre-seed the tracker with a peak > 5000 so floor = 4_800.
        tracker.update(current_equity_usd=5_300.0)
        assert tracker.floor_usd() == pytest.approx(4_800.0)

        # Configure a generous cushion so even a small dip fires preempt.
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        cfg.kill_switch = {
            "global": {
                "max_drawdown_kill_pct_of_portfolio": 100.0,
                "daily_loss_cap_pct_of_portfolio": 100.0,
            },
            "tier_a": {
                "per_bucket": {},
                "apex_eval_preemptive": {"cushion_usd": 400},
            },
            "tier_b": {
                "per_bucket": {},
                "correlation_kill": {"enabled": False},
                "funding_veto": {"soft_threshold_bps": 20, "hard_threshold_bps": 50},
            },
        }

        # FakeBot default equity is 5_000 -> distance to floor 4_800 is 200 USD.
        # 200 < cushion=400 -> FLATTEN_TIER_A_PREEMPTIVE must fire.
        latch_path = tmp_path / "state" / "kill_switch_latch.json"
        latch = KillSwitchLatch(latch_path)
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            kill_switch=mod.KillSwitch(cfg.kill_switch),
            kill_switch_latch=latch,
            trailing_dd_tracker=tracker,
        )
        await runtime.run()

        # After the run, the latch must be TRIPPED on disk.
        persisted = KillSwitchLatch(latch_path).read()
        assert persisted.state is LatchState.TRIPPED
        assert persisted.action == KillAction.FLATTEN_TIER_A_PREEMPTIVE.value

    @pytest.mark.asyncio
    async def test_tracker_state_survives_runtime_restart(self, tmp_path):
        """Two successive runtimes share the tracker's persisted peak."""
        tracker_path = tmp_path / "state" / "trailing_dd.json"
        # First run: tracker sees 5_000 equity, peak stays at 5_000.
        tracker_a = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=5_000.0,
            trailing_dd_cap_usd=500.0,
        )
        tracker_a.update(current_equity_usd=5_275.0)  # set a peak
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        rt1 = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(), trailing_dd_tracker=tracker_a,
        )
        await rt1.run()

        # Second tracker instance loaded from the same path.
        tracker_b = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=5_000.0,
            trailing_dd_cap_usd=500.0,
        )
        assert tracker_b.state().peak_equity_usd == 5_275.0


# --------------------------------------------------------------------------- #
# D3 -- ConsistencyGuard integration (advisory 30% rule)
#
# The guard is advisory: it records today's realized tier-A PnL each loop and
# emits a status-transition alert when the largest-winning-day ratio climbs
# into WARNING or VIOLATION. No force-flatten. These tests cover:
#   * no guard attached = no mutation and no alert
#   * guard attached = today's entry recorded each tick
#   * pre-seeded near-violation prior history -> tick drives VIOLATION, alert
#     fires exactly once (not every tick while we stay in state)
# --------------------------------------------------------------------------- #
class TestConsistencyGuardIntegration:
    @pytest.mark.asyncio
    async def test_no_guard_is_noop(self, tmp_path):
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert runtime.consistency_guard is None
        rc = await runtime.run()
        assert rc == 0

    @pytest.mark.asyncio
    async def test_guard_records_today_from_session_pnl(self, tmp_path):
        """Runtime feeds today's tier-A session_realized_pnl into the guard."""
        guard_path = tmp_path / "state" / "consistency.json"
        guard = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        bindings = _fake_bindings()

        # Stamp a realized PnL on the MNQ bot so the guard ingests
        # a non-zero value.
        def _mnq_with_pnl() -> FakeBot:
            b = FakeBot("mnq", "MNQ", "A")
            b.state.todays_pnl = 250.0
            return b
        bindings[0] = mod.BotBinding(
            "mnq", "A", "tier_a_mnq_live", _mnq_with_pnl, "MNQ",
        )

        runtime = mod.ApexRuntime(
            cfg, bindings=bindings, consistency_guard=guard,
        )
        rc = await runtime.run()
        assert rc == 0
        # One day entry for today, value = 250 (from the fake bot's pnl).
        days = guard.state().days
        assert len(days) == 1
        only_date = next(iter(days))
        assert days[only_date] == pytest.approx(250.0)

    @pytest.mark.asyncio
    async def test_guard_emits_violation_transition_alert(self, tmp_path):
        """Pre-seed a history that tips into VIOLATION on first tick."""
        guard_path = tmp_path / "state" / "consistency.json"
        guard = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        # Seed 4 prior days of small profits -> total prior=1000.
        # Today's PnL of 1000 will make largest=1000, total=2000,
        # ratio=50% -> VIOLATION.
        guard.record_eod("2026-04-20", 250.0)
        guard.record_eod("2026-04-21", 250.0)
        guard.record_eod("2026-04-22", 250.0)
        guard.record_eod("2026-04-23", 250.0)

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        # Allow the consistency_status alert to route (even if unknown,
        # the dispatcher logs it without exception).
        bindings = _fake_bindings()

        def _mnq_with_big_pnl() -> FakeBot:
            b = FakeBot("mnq", "MNQ", "A")
            b.state.todays_pnl = 1000.0
            return b
        bindings[0] = mod.BotBinding(
            "mnq", "A", "tier_a_mnq_live", _mnq_with_big_pnl, "MNQ",
        )

        runtime = mod.ApexRuntime(
            cfg, bindings=bindings, consistency_guard=guard,
        )
        await runtime.run()

        # Verdict is VIOLATION from tick 1 onward.
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.VIOLATION

        # Exactly ONE consistency_status log line (transition fires once,
        # subsequent ticks are steady state).
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        kinds = [json.loads(line)["kind"] for line in lines]
        assert kinds.count("consistency_status") == 1


# --------------------------------------------------------------------------- #
# Blocker #2 -- live-mode gate on TrailingDDTracker presence.
#
# The legacy build_apex_eval_snapshot() fallback does not implement the
# Apex freeze rule. Running live without the tick-precise tracker risks a
# silent eval bust when equity retraces past the un-frozen floor. The
# ApexRuntime constructor therefore refuses to build in live mode without
# a tracker. Dry-run, paper-sim, and tests stay permissive.
# --------------------------------------------------------------------------- #
class TestLiveModeTrackerGate:
    def test_live_mode_without_tracker_raises(self, tmp_path):
        cfg = _cfg_factory(
            tmp_path,
            go_state={"tier_a_mnq_live": True},
            live=True,
            dry_run=False,
        )
        with pytest.raises(RuntimeError, match="TrailingDDTracker"):
            mod.ApexRuntime(cfg, bindings=_fake_bindings())

    def test_live_mode_with_tracker_builds_cleanly(self, tmp_path):
        tracker = TrailingDDTracker.load_or_init(
            path=tmp_path / "state" / "trailing_dd.json",
            starting_balance_usd=5_000.0,
            trailing_dd_cap_usd=500.0,
        )
        cfg = _cfg_factory(
            tmp_path,
            go_state={"tier_a_mnq_live": True},
            live=True,
            dry_run=False,
        )
        # Construction must succeed. Use a MockRouter to avoid the
        # real-router branch (no creds in test env anyway).
        runtime = mod.ApexRuntime(
            cfg,
            bindings=_fake_bindings(),
            trailing_dd_tracker=tracker,
            router=mod.MockRouter(log_path=tmp_path / "orders.jsonl"),
        )
        assert runtime.trailing_dd_tracker is tracker

    def test_dry_run_without_tracker_builds_cleanly(self, tmp_path):
        """Dry-run is exempt from the gate: legacy proxy is fine for paper."""
        cfg = _cfg_factory(
            tmp_path,
            go_state={"tier_a_mnq_live": True},
            live=False,
            dry_run=True,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert runtime.trailing_dd_tracker is None

    def test_live_flag_alone_is_not_enough_dry_run_still_wins(self, tmp_path):
        """When dry_run=True overrides live=True, no tracker required."""
        cfg = _cfg_factory(
            tmp_path,
            go_state={"tier_a_mnq_live": True},
            live=True,
            dry_run=True,  # dry_run wins; legacy proxy is safe here
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert runtime.trailing_dd_tracker is None


# --------------------------------------------------------------------------- #
# Blocker #3 -- ConsistencyGuard VIOLATION enforces PAUSE_NEW_ENTRIES.
#
# The advisory-only path (alert + log, no action) was the Red Team's top D3
# finding: a silent log is not enforcement. The runtime now synthesizes a
# KillVerdict(PAUSE_NEW_ENTRIES, tier_a, CRITICAL) on VIOLATION and pushes
# it through apply_verdict, which flips bot.state.is_paused for every
# tier-A bot. Tests cover: pause flag set on VIOLATION, verdict logged in
# runtime.jsonl, no flatten (positions allowed to close naturally).
# --------------------------------------------------------------------------- #
class TestConsistencyViolationPauses:
    @pytest.mark.asyncio
    async def test_violation_pauses_tier_a_bots(self, tmp_path):
        guard_path = tmp_path / "state" / "consistency.json"
        guard = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        # Pre-seed history that is already in violation so the tick
        # immediately flips the verdict. Use 2023 dates to avoid any
        # collision with apex_trading_day_iso() for "today".
        guard.record_eod("2023-01-01", 1_000.0)
        guard.record_eod("2023-01-02", 100.0)
        v0 = guard.evaluate()
        assert v0.status is ConsistencyStatus.VIOLATION

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        bindings = _fake_bindings()

        captured_bots: dict[str, FakeBot] = {}

        def _mnq_in_violation() -> FakeBot:
            b = FakeBot("mnq", "MNQ", "A")
            # today's tier-A PnL is neutral; the seeded history alone
            # drives VIOLATION on tick.
            b.state.todays_pnl = 0.0
            captured_bots["mnq"] = b
            return b
        bindings[0] = mod.BotBinding(
            "mnq", "A", "tier_a_mnq_live", _mnq_in_violation, "MNQ",
        )

        runtime = mod.ApexRuntime(
            cfg, bindings=bindings, consistency_guard=guard,
        )
        await runtime.run()

        # The tier-A bot is now paused.
        assert captured_bots["mnq"].state.is_paused is True

        # The verdict is persisted in runtime.jsonl under the tick entry
        # for the same bar. _log flattens meta kwargs directly into the
        # entry, so "verdicts" is a top-level key.
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        events = [json.loads(line) for line in lines]
        tick_entries = [e for e in events if e["kind"] == "tick"]
        assert tick_entries, "expected at least one tick log entry"
        last = tick_entries[-1]
        actions = [v["action"] for v in last["verdicts"]]
        assert "PAUSE_NEW_ENTRIES" in actions

    @pytest.mark.asyncio
    async def test_warning_does_not_pause(self, tmp_path):
        """WARNING is advisory only. No bot paused, no synthetic verdict.

        Pre-seed dates are in 2023 so they cannot collide with today's
        apex_trading_day_iso() value (which is what the runtime writes
        on each tick -- if a pre-seed date matched today's key, the
        runtime would overwrite it and shift the ratio).
        """
        guard_path = tmp_path / "state" / "consistency.json"
        guard = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        # Ratio 500/1800 = 27.8% -> between 25% and 30% -> WARNING.
        guard.record_eod("2023-01-01", 500.0)
        guard.record_eod("2023-01-02", 500.0)
        guard.record_eod("2023-01-03", 400.0)
        guard.record_eod("2023-01-04", 400.0)
        v0 = guard.evaluate()
        assert v0.status is ConsistencyStatus.WARNING

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        captured: dict[str, FakeBot] = {}

        def _mnq() -> FakeBot:
            b = FakeBot("mnq", "MNQ", "A")
            captured["mnq"] = b
            return b
        bindings = _fake_bindings()
        bindings[0] = mod.BotBinding(
            "mnq", "A", "tier_a_mnq_live", _mnq, "MNQ",
        )

        runtime = mod.ApexRuntime(
            cfg, bindings=bindings, consistency_guard=guard,
        )
        await runtime.run()

        # Paused flag still False.
        assert captured["mnq"].state.is_paused is False

        # No PAUSE_NEW_ENTRIES verdict in any tick entry. verdicts list
        # is a top-level key on the tick entry (see _log flattening).
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        events = [json.loads(line) for line in lines]
        actions: list[str] = []
        for e in events:
            if e["kind"] == "tick":
                actions.extend(v["action"] for v in e["verdicts"])
        assert "PAUSE_NEW_ENTRIES" not in actions


# --------------------------------------------------------------------------- #
# R1 end-to-end -- BrokerEquityPoller + BrokerEquityReconciler integration.
#
# The reconciler is observation-only: every tick, it compares the runtime's
# tier-A aggregate equity against the broker-polled MTM and classifies the
# drift. The runtime surfaces each classification in the tick log and fires
# a `broker_equity_drift` alert on the transition INTO broker_below_logical.
# These tests cover:
#   * no reconciler attached = legacy path, no broker_equity key in tick
#   * reconciler + poller attached = each tick logs a classification
#   * tick-level alert fires exactly once on transition into drift state
#   * no-broker-data classification is logged without alert
# --------------------------------------------------------------------------- #
class TestBrokerEquityReconcilerIntegration:
    @pytest.mark.asyncio
    async def test_no_reconciler_attached_is_noop(self, tmp_path):
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=1,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert runtime.broker_equity_reconciler is None
        assert runtime.broker_equity_poller is None
        rc = await runtime.run()
        assert rc == 0
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        tick_entries = [
            json.loads(ln) for ln in lines
            if json.loads(ln)["kind"] == "tick"
        ]
        assert tick_entries
        # No broker_equity sub-key on any tick entry.
        for e in tick_entries:
            assert "broker_equity" not in e

    @pytest.mark.asyncio
    async def test_reconciler_logs_classification_each_tick(self, tmp_path):
        """Attach a reconciler with a static-value source -- every tick must
        record a broker_equity block in the tick log."""
        # Within-tolerance: broker == logical (5000 tier-A from FakeBot).
        def _src() -> float | None:
            return 5_000.0
        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
            tolerance_pct=0.001,
        )
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
        )
        rc = await runtime.run()
        assert rc == 0
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        tick_entries = [
            json.loads(ln) for ln in lines
            if json.loads(ln)["kind"] == "tick"
        ]
        assert len(tick_entries) == 2
        for e in tick_entries:
            be = e["broker_equity"]
            assert be["reason"] == "within_tolerance"
            assert be["in_tolerance"] is True
            assert be["drift_usd"] == pytest.approx(0.0)
        # Stats on the reconciler reflect two ticks of checks.
        assert rec.stats.checks_total == 2
        assert rec.stats.checks_in_tolerance == 2
        assert rec.stats.checks_out_of_tolerance == 0

    @pytest.mark.asyncio
    async def test_reconciler_alert_fires_once_on_drift_transition(
        self, tmp_path,
    ):
        """Pre-stage a broker source that reports LOWER than logical so the
        first tick classifies as broker_below_logical and fires the alert.
        Subsequent ticks (still in drift) must NOT re-fire the alert."""
        # Broker reports 4000 vs logical 5000 -> drift_usd=1000 -> well past
        # 50 USD / 0.1% tolerance -> broker_below_logical.
        def _low() -> float | None:
            return 4_000.0
        rec = BrokerEquityReconciler(
            broker_equity_source=_low,
            tolerance_usd=50.0,
            tolerance_pct=0.001,
        )

        # Stub dispatcher so we can count broker_equity_drift emissions.
        disp = _StubDispatcher()
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            dispatcher=disp,
        )
        rc = await runtime.run()
        assert rc == 0
        # Exactly ONE broker_equity_drift alert across 3 ticks (transition).
        drift_events = [
            payload for (event, payload) in disp.sent
            if event == "broker_equity_drift"
        ]
        assert len(drift_events) == 1
        evt = drift_events[0]
        assert evt["reason"] == "broker_below_logical"
        assert evt["logical_equity_usd"] == pytest.approx(5_000.0)
        assert evt["broker_equity_usd"] == pytest.approx(4_000.0)
        assert evt["drift_usd"] == pytest.approx(1_000.0)

        # All three tick logs carry the classification.
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        tick_entries = [
            json.loads(ln) for ln in lines
            if json.loads(ln)["kind"] == "tick"
        ]
        assert len(tick_entries) == 3
        for e in tick_entries:
            assert e["broker_equity"]["reason"] == "broker_below_logical"
            assert e["broker_equity"]["in_tolerance"] is False
        # Stats match.
        assert rec.stats.checks_total == 3
        assert rec.stats.checks_out_of_tolerance == 3

    @pytest.mark.asyncio
    async def test_reconciler_no_broker_data_is_logged_not_alerted(
        self, tmp_path,
    ):
        """A source that always returns None classifies as no_broker_data
        every tick. This is the paper / dormant-adapter path. The tick log
        records it; the alert channel stays silent."""
        def _none() -> float | None:
            return None
        rec = BrokerEquityReconciler(broker_equity_source=_none)
        disp = _StubDispatcher()
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            dispatcher=disp,
        )
        await runtime.run()
        assert rec.stats.checks_total == 2
        assert rec.stats.checks_no_data == 2
        # No broker_equity_drift alert.
        assert not any(
            event == "broker_equity_drift" for (event, _) in disp.sent
        )
        # Every tick logs reason=no_broker_data.
        lines = (tmp_path / "runtime.jsonl").read_text(
            encoding="utf-8",
        ).strip().splitlines()
        tick_entries = [
            json.loads(ln) for ln in lines
            if json.loads(ln)["kind"] == "tick"
        ]
        for e in tick_entries:
            assert e["broker_equity"]["reason"] == "no_broker_data"
            assert e["broker_equity"]["drift_usd"] is None

    @pytest.mark.asyncio
    async def test_poller_lifecycle_started_and_stopped_by_runtime(
        self, tmp_path,
    ):
        """When a poller is wired, run() awaits its start() and the finally
        block awaits stop(). We verify via the poller's own is_running()
        and fetch counters."""
        fetches = 0

        async def _fetch() -> float | None:
            nonlocal fetches
            fetches += 1
            return 5_000.0

        poller = BrokerEquityPoller(
            name="test",
            fetch_fn=_fetch,
            refresh_s=0.05,
            stale_after_s=5.0,
        )
        rec = BrokerEquityReconciler(
            broker_equity_source=poller.current,
            tolerance_usd=50.0,
        )
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            broker_equity_poller=poller,
        )
        assert not poller.is_running()
        await runtime.run()
        # Poller was stopped cleanly by the finally block.
        assert not poller.is_running()
        # start() does one eager fetch before scheduling the loop, so at
        # minimum one fetch must have happened.
        assert fetches >= 1
        # Reconciler received the cached value on every tick (2 checks).
        assert rec.stats.checks_total == 2
        # And because fetch returns 5000 == logical, all within tolerance.
        assert rec.stats.checks_in_tolerance == 2

    @pytest.mark.asyncio
    async def test_drift_transition_resets_and_refires(self, tmp_path):
        """Transition tracking: drift clears -> re-enters -> alert fires
        twice, not once. Uses a mutable closure to swap the broker value
        between ticks."""
        broker_value: list[float] = [5_000.0]  # tick 0: in tolerance

        def _src() -> float | None:
            return broker_value[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
            tolerance_pct=0.001,
        )

        # Patch dispatcher send to mutate broker_value between ticks so we
        # can script the transition pattern. The tick cadence is tight
        # (tick_interval_s=0.0) so ticks advance as fast as the loop.
        disp = _StubDispatcher()
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=4,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            dispatcher=disp,
        )
        # Drive the ticks manually so we can script broker_value.
        bots_inst = runtime._instantiate_active_bots()
        for _b, bot in bots_inst:
            await bot.start()
        # tick 0: within tolerance (5000 vs 5000)
        await runtime._tick(bots_inst, 0)
        # tick 1: drift
        broker_value[0] = 4_000.0
        await runtime._tick(bots_inst, 1)
        # tick 2: recover
        broker_value[0] = 5_000.0
        await runtime._tick(bots_inst, 2)
        # tick 3: re-drift
        broker_value[0] = 4_000.0
        await runtime._tick(bots_inst, 3)
        for _, bot in bots_inst:
            await bot.stop()

        drift_events = [
            payload for (event, payload) in disp.sent
            if event == "broker_equity_drift"
        ]
        # H3 closure (v0.1.66): three alerts now fire across this
        # sequence -- the original two transition entries (tick 1, tick
        # 3) plus the new "recovered" alert (tick 2). The transition
        # count itself is unchanged; H3 just adds an explicit recovery
        # signal so the operator knows the drift cleared rather than
        # silently disappearing.
        kinds = [p.get("kind") for p in drift_events]
        assert kinds.count("transition") == 2
        assert kinds.count("recovered") == 1
        assert len(drift_events) == 3


# ---------------------------------------------------------------------------
# v0.1.65 -- H6 closure: live mode refuse-to-boot when no real broker source
# ---------------------------------------------------------------------------


class TestBuildBrokerEquityAdapterLiveModeGate:
    """v0.1.65 H6 -- live + no-creds = refuse-to-boot unless opted in."""

    def test_paper_mode_returns_null_adapter(self) -> None:
        from apex_predator.scripts.run_apex_live import (
            _build_broker_equity_adapter,
        )

        adapter = _build_broker_equity_adapter(live=False, dry_run=True)
        # Wrapped in SafeBrokerEquityAdapter (H7), so the inner Null
        # is reached via the wrapper. The wrapper still satisfies the
        # protocol and reports a name we can grep on.
        assert "paper-null" in adapter.name

    def test_dry_run_with_live_flag_returns_null(self) -> None:
        from apex_predator.scripts.run_apex_live import (
            _build_broker_equity_adapter,
        )

        # cfg.live=True but cfg.dry_run=True wins -- this is the
        # paper smoke path. Should not consult broker creds.
        adapter = _build_broker_equity_adapter(live=True, dry_run=True)
        assert "paper-null" in adapter.name

    def test_live_no_creds_strict_raises_boot_refused(
        self, monkeypatch,
    ) -> None:
        """The default (allow_live_no_drift=False) refuses to boot."""
        from apex_predator.core.broker_equity_adapter import (
            BrokerEquityNotAvailableError,
        )
        from apex_predator.scripts.run_apex_live import (
            _build_broker_equity_adapter,
        )
        from apex_predator.venues.ibkr import IbkrClientPortalVenue
        from apex_predator.venues.tastytrade import TastytradeVenue

        monkeypatch.setattr(
            IbkrClientPortalVenue, "has_credentials",
            lambda self: False,
        )
        monkeypatch.setattr(
            TastytradeVenue, "has_credentials",
            lambda self: False,
        )
        with pytest.raises(BrokerEquityNotAvailableError, match="no real broker"):
            _build_broker_equity_adapter(live=True, dry_run=False)

    def test_live_no_creds_allow_returns_null_with_warn(
        self, monkeypatch, caplog,
    ) -> None:
        """The opt-in path returns Null but logs a loud WARN."""
        import logging as _logging

        from apex_predator.scripts.run_apex_live import (
            _build_broker_equity_adapter,
        )
        from apex_predator.venues.ibkr import IbkrClientPortalVenue
        from apex_predator.venues.tastytrade import TastytradeVenue

        monkeypatch.setattr(
            IbkrClientPortalVenue, "has_credentials",
            lambda self: False,
        )
        monkeypatch.setattr(
            TastytradeVenue, "has_credentials",
            lambda self: False,
        )
        with caplog.at_level(_logging.WARNING):
            adapter = _build_broker_equity_adapter(
                live=True, dry_run=False, allow_live_no_drift=True,
            )
        assert "live-null-no-creds" in adapter.name
        # Confirm the loud WARN fired (operator-visible signal that
        # drift detection is OFF).
        warns = [
            r.message for r in caplog.records if r.levelno >= _logging.WARNING
        ]
        assert any("APEX_ALLOW_LIVE_NO_DRIFT" in m for m in warns)

    def test_live_with_ibkr_creds_returns_ibkr_adapter(
        self, monkeypatch,
    ) -> None:
        from apex_predator.scripts.run_apex_live import (
            _build_broker_equity_adapter,
        )
        from apex_predator.venues.ibkr import IbkrClientPortalVenue

        monkeypatch.setattr(
            IbkrClientPortalVenue, "has_credentials",
            lambda self: True,
        )
        adapter = _build_broker_equity_adapter(live=True, dry_run=False)
        # SafeBrokerEquityAdapter wraps it -- name reflects wrapping.
        assert "safe(" in adapter.name
        assert "ibkr" in adapter.name

    def test_live_ibkr_missing_tasty_creds_returns_tasty(
        self, monkeypatch,
    ) -> None:
        from apex_predator.scripts.run_apex_live import (
            _build_broker_equity_adapter,
        )
        from apex_predator.venues.ibkr import IbkrClientPortalVenue
        from apex_predator.venues.tastytrade import TastytradeVenue

        monkeypatch.setattr(
            IbkrClientPortalVenue, "has_credentials",
            lambda self: False,
        )
        monkeypatch.setattr(
            TastytradeVenue, "has_credentials",
            lambda self: True,
        )
        adapter = _build_broker_equity_adapter(live=True, dry_run=False)
        # Tasty wins on the fallback path.
        assert "tastytrade" in adapter.name


# ---------------------------------------------------------------------------
# v0.1.66 H3 -- sustained-drift re-fire interval in the runtime
# ---------------------------------------------------------------------------


class TestSustainedDriftReAlert:
    """v0.1.66 H3 -- re-fire after broker_drift_realert_interval_s."""

    @pytest.mark.asyncio
    async def test_sustained_drift_re_alerts_after_interval(self, tmp_path):
        """Tick 0 enters drift; tick 1 stays in drift past the interval
        and fires a sustained alert; tick 2 stays in drift but inside
        the interval and stays silent."""
        from apex_predator.core.broker_equity_reconciler import (
            BrokerEquityReconciler,
        )

        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 4_000.0,
            tolerance_usd=50.0,
            tolerance_pct=0.001,
        )
        disp = _StubDispatcher()
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            dispatcher=disp,
        )
        # Drop the re-alert interval so we can test sustained behaviour
        # without sleeping. Default is 1800s (30 min).
        runtime.broker_drift_realert_interval_s = 0.001

        bots_inst = runtime._instantiate_active_bots()
        for _b, bot in bots_inst:
            await bot.start()
        # tick 0: enter drift -> "transition" alert
        await runtime._tick(bots_inst, 0)
        # Advance time so the re-alert interval has elapsed.
        import time as _time
        _time.sleep(0.005)
        # tick 1: still drifting + interval elapsed -> "sustained" alert
        await runtime._tick(bots_inst, 1)
        # Re-arm the interval to a huge value so tick 2 cannot fire
        # another sustained alert (we are pinning the "did sustain
        # actually fire once" semantic, not "did tick 2 fire too").
        runtime.broker_drift_realert_interval_s = 1_000_000.0
        await runtime._tick(bots_inst, 2)
        for _, bot in bots_inst:
            await bot.stop()

        drift_events = [
            payload for (event, payload) in disp.sent
            if event == "broker_equity_drift"
        ]
        kinds = [p.get("kind") for p in drift_events]
        assert kinds.count("transition") == 1
        assert kinds.count("sustained") == 1
        # Two alerts total: transition (tick 0) + sustained (tick 1).
        # Tick 2 is suppressed because the interval was bumped back up.
        assert len(drift_events) == 2

    @pytest.mark.asyncio
    async def test_sustained_drift_silent_within_interval(self, tmp_path):
        """No re-alert when drift persists but the interval has not elapsed."""
        from apex_predator.core.broker_equity_reconciler import (
            BrokerEquityReconciler,
        )

        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 4_000.0,
            tolerance_usd=50.0,
        )
        disp = _StubDispatcher()
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            dispatcher=disp,
        )
        # Set the interval to something huge so no re-alert can fire.
        runtime.broker_drift_realert_interval_s = 1_000_000.0

        bots_inst = runtime._instantiate_active_bots()
        for _b, bot in bots_inst:
            await bot.start()
        for i in range(3):
            await runtime._tick(bots_inst, i)
        for _, bot in bots_inst:
            await bot.stop()

        drift_events = [
            (event, payload) for (event, payload) in disp.sent
            if event == "broker_equity_drift"
        ]
        # Only the transition alert; no sustained alert because the
        # interval is effectively infinite.
        assert len(drift_events) == 1
        assert drift_events[0][1]["kind"] == "transition"

    @pytest.mark.asyncio
    async def test_recovery_emits_recovered_kind(self, tmp_path):
        """Drift -> recovery emits a 'recovered' alert that resets the
        sustained-alert ts so a future drift fires a fresh transition."""
        from apex_predator.core.broker_equity_reconciler import (
            BrokerEquityReconciler,
        )

        broker = [4_000.0]  # tick 0: drifting

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
        )
        disp = _StubDispatcher()
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            broker_equity_reconciler=rec,
            dispatcher=disp,
        )

        bots_inst = runtime._instantiate_active_bots()
        for _b, bot in bots_inst:
            await bot.start()
        # tick 0: drifting -> transition alert
        await runtime._tick(bots_inst, 0)
        broker[0] = 5_000.0  # recover
        # tick 1: clean recovery -> recovered alert
        await runtime._tick(bots_inst, 1)
        for _, bot in bots_inst:
            await bot.stop()

        drift_events = [
            payload for (event, payload) in disp.sent
            if event == "broker_equity_drift"
        ]
        kinds = [p.get("kind") for p in drift_events]
        assert kinds == ["transition", "recovered"]
        # After recovery the ts is reset to None so a fresh entry
        # fires immediately as a new transition.
        assert runtime._last_broker_drift_alert_ts is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# v0.1.67 M3 -- runtime log rotator wiring
# ---------------------------------------------------------------------------


class TestRuntimeLogRotatorWiring:
    """v0.1.67 M3 -- ApexRuntime calls rotator.run() periodically."""

    def test_default_rotator_constructed_when_kwarg_omitted(self, tmp_path):
        from apex_predator.core.runtime_log_rotator import RuntimeLogRotator

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=0,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert isinstance(runtime.runtime_log_rotator, RuntimeLogRotator)
        assert runtime.runtime_log_rotator.log_path == cfg.log_path
        assert runtime.runtime_log_rotate_every_n_ticks == 600

    def test_explicit_rotator_kwarg_overrides_default(self, tmp_path):
        from apex_predator.core.runtime_log_rotator import RuntimeLogRotator

        custom = RuntimeLogRotator(
            log_path=tmp_path / "custom.jsonl",
            rotate_at_size_bytes=1024,
        )
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=0,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            runtime_log_rotator=custom,
        )
        assert runtime.runtime_log_rotator is custom

    @pytest.mark.asyncio
    async def test_rotation_fires_on_size_threshold(self, tmp_path):
        """When the log exceeds the rotator threshold mid-run, the
        rotator renames it aside and the runtime keeps writing fresh."""
        from apex_predator.core.runtime_log_rotator import RuntimeLogRotator

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        # Seed the live log so it's already past the threshold; with a
        # very small threshold (100 bytes) and N=1 rotate-every-tick,
        # the first tick triggers rotation.
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.log_path.write_bytes(b"x" * 5000)
        rotator = RuntimeLogRotator(
            log_path=cfg.log_path, rotate_at_size_bytes=100,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            runtime_log_rotator=rotator,
        )
        runtime.runtime_log_rotate_every_n_ticks = 1

        rc = await runtime.run()
        assert rc == 0
        # At least one rotation should have happened.
        assert rotator.stats.rotations >= 1
        # The rotated archive lives next to the live log.
        archives = list(
            cfg.log_path.parent.glob(f"{cfg.log_path.stem}.*.jsonl"),
        )
        assert archives, "expected at least one rotated archive"

    @pytest.mark.asyncio
    async def test_rotation_emits_log_rotation_event(self, tmp_path):
        """When rotator.run yields a non-empty outcome, the runtime
        records a 'log_rotation' kind entry."""
        import json

        from apex_predator.core.runtime_log_rotator import RuntimeLogRotator

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.log_path.write_bytes(b"x" * 5000)
        rotator = RuntimeLogRotator(
            log_path=cfg.log_path, rotate_at_size_bytes=100,
        )
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(),
            runtime_log_rotator=rotator,
        )
        runtime.runtime_log_rotate_every_n_ticks = 1

        await runtime.run()
        # The new live log (post-rotation) should carry a log_rotation
        # entry recording the archive that was rolled aside.
        if cfg.log_path.exists():
            lines = cfg.log_path.read_text(encoding="utf-8").splitlines()
            kinds = [
                json.loads(ln).get("kind")
                for ln in lines if ln.strip()
            ]
            assert "log_rotation" in kinds


# ---------------------------------------------------------------------------
# v0.1.69 B3 -- tier-A aggregate-equity invariant wiring
# ---------------------------------------------------------------------------


class TestTierAInvariantWiring:
    """v0.1.69 B3 -- ApexRuntime fires tier_a_invariant_violation on bad agg."""

    @pytest.mark.asyncio
    async def test_oversize_aggregate_fires_alert_once(self, tmp_path):
        """Two tier-A bots both at full account size -> one alert
        across multiple ticks (transition-only latch)."""
        from apex_predator.core.kill_switch_runtime import BotSnapshot
        from apex_predator.scripts.run_apex_live import BotBinding

        original = mod.build_bot_snapshot

        def _fat_snapshot(b, bot):
            snap = original(b, bot)
            return BotSnapshot(
                name=snap.name,
                tier=snap.tier,
                equity_usd=50_000.0 if snap.tier == "A" else snap.equity_usd,
                peak_equity_usd=snap.peak_equity_usd,
                session_realized_pnl_usd=snap.session_realized_pnl_usd,
                consecutive_losses=snap.consecutive_losses,
                open_position_count=snap.open_position_count,
            )

        bindings = _fake_bindings()
        first = bindings[0]
        bindings = [
            BotBinding(
                name=first.name,
                tier=first.tier,
                flag=first.flag,
                factory=first.factory,
                symbol=first.symbol,
            ),
            BotBinding(
                name="nq_test",
                tier="A",
                flag=first.flag,
                factory=first.factory,
                symbol=first.symbol,
            ),
        ]

        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3,
        )
        cfg.tier_a_account_size_usd = 50_000.0
        disp = _StubDispatcher()
        runtime = mod.ApexRuntime(
            cfg, bindings=bindings, dispatcher=disp,
        )
        mod.build_bot_snapshot = _fat_snapshot
        try:
            await runtime.run()
        finally:
            mod.build_bot_snapshot = original

        violations = [
            payload for (event, payload) in disp.sent
            if event == "tier_a_invariant_violation"
        ]
        # Exactly ONE alert across 3 ticks (transition-only latch).
        assert len(violations) == 1
        assert violations[0]["verdict"] == "oversize_aggregate"
        assert violations[0]["sum_logical_usd"] == pytest.approx(100_000.0)

    @pytest.mark.asyncio
    async def test_normal_aggregate_does_not_fire_alert(self, tmp_path):
        """Single tier-A bot tracking the full account size is FINE;
        no violation should fire."""
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=2,
        )
        cfg.tier_a_account_size_usd = 50_000.0
        disp = _StubDispatcher()
        runtime = mod.ApexRuntime(
            cfg, bindings=_fake_bindings(), dispatcher=disp,
        )
        await runtime.run()

        violations = [
            event for (event, _) in disp.sent
            if event == "tier_a_invariant_violation"
        ]
        assert len(violations) == 0

    def test_invariant_verdict_attribute_initialised(self, tmp_path):
        """ApexRuntime initialises the per-tick verdict cache to None."""
        cfg = _cfg_factory(
            tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=0,
        )
        runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
        assert runtime._last_tier_a_invariant_verdict is None  # noqa: SLF001

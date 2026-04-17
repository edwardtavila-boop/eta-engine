"""Tests for scripts.run_eta_live — the tie-together runtime loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from eta_engine.core.kill_switch_runtime import (
    ApexEvalSnapshot,
    BotSnapshot,
    CorrelationSnapshot,
    FundingSnapshot,
    KillAction,
    KillSeverity,
    KillVerdict,
    PortfolioSnapshot,
)
from eta_engine.obs.alert_dispatcher import AlertDispatcher
from eta_engine.scripts import run_eta_live as mod


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

    def __post_init__(self):
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
    snap = mod.build_bot_snapshot(binding, bot)
    assert snap.name == "mnq"
    assert snap.tier == "A"
    assert snap.equity_usd == 4800
    assert snap.peak_equity_usd == 5000
    assert snap.session_realized_pnl_usd == -200
    assert snap.consecutive_losses == 2


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
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def send(self, event: str, payload: dict):
        self.sent.append((event, payload))
        return None


class _StubRouter:
    name = "stub"

    def __init__(self):
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
    rep = await mod.apply_verdict(v, [mnq], router, disp)
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
    rep = await mod.apply_verdict(v, [eth], router, disp)
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
    rep = await mod.apply_verdict(v, [mnq, eth], router, disp)
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
    rep = await mod.apply_verdict(v, [mnq, eth], router, disp)
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
    rep = await mod.apply_verdict(v, [mnq, nq, eth], router, disp)
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
    rep = await mod.apply_verdict(v, [eth], router, disp)
    assert eth[1].config.risk_per_trade_pct == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_apply_verdict_pause_new_entries():
    disp = _StubDispatcher()
    router = _StubRouter()
    bindings = _fake_bindings()
    mnq = (bindings[0], FakeBot("mnq", "MNQ", "A"))
    v = KillVerdict(action=KillAction.PAUSE_NEW_ENTRIES, severity=KillSeverity.WARN,
                    reason="warn", scope="bot:mnq")
    rep = await mod.apply_verdict(v, [mnq], router, disp)
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
    kinds = [json.loads(l)["kind"] for l in lines]
    assert "no_active_bots" in kinds


@pytest.mark.asyncio
async def test_runtime_ticks_active_bot(tmp_path):
    cfg = _cfg_factory(tmp_path, go_state={"tier_a_mnq_live": True}, max_bars=3)
    runtime = mod.ApexRuntime(cfg, bindings=_fake_bindings())
    rc = await runtime.run()
    assert rc == 0
    lines = (tmp_path / "runtime.jsonl").read_text(encoding="utf-8").strip().splitlines()
    tick_entries = [json.loads(l) for l in lines if json.loads(l)["kind"] == "tick"]
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
    kinds = [json.loads(l)["kind"] for l in lines]
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
    for l in lines:
        e = json.loads(l)
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
# Router selection: real vs mock
# --------------------------------------------------------------------------- #
def test_tradovate_creds_absent_by_default(monkeypatch):
    for k in ("TRADOVATE_CLIENT_ID", "TRADOVATE_CLIENT_SECRET",
              "TRADOVATE_USERNAME", "TRADOVATE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert mod._tradovate_creds_present() is False


def test_tradovate_creds_present_with_env(monkeypatch):
    monkeypatch.setenv("TRADOVATE_CLIENT_ID", "x")
    monkeypatch.setenv("TRADOVATE_CLIENT_SECRET", "x")
    monkeypatch.setenv("TRADOVATE_USERNAME", "x")
    monkeypatch.setenv("TRADOVATE_PASSWORD", "x")
    assert mod._tradovate_creds_present() is True


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

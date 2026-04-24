"""
APEX PREDATOR  //  scripts.run_apex_live
========================================
The runtime loop that ties every piece together.

Reads
-----
* configs/tradovate.yaml, configs/bybit.yaml, configs/kill_switch.yaml,
  configs/alerts.yaml
* .env (via os.environ; we do not ship python-dotenv — it's optional)
* roadmap_state.json -> shared_artifacts.apex_go_state  (set by go_trigger.py)

Runs
----
For each configured bot whose apex_go_state flag is True:
    1. Build a BotSnapshot from bot.state.
    2. Aggregate PortfolioSnapshot / FundingSnapshot / CorrelationSnapshot /
       ApexEvalSnapshot from the running fleet.
    3. Call KillSwitch.evaluate(...).
    4. Act on verdicts:
         - CONTINUE               → noop
         - HALVE_SIZE             → bot.config.risk_per_trade_pct /= 2
         - PAUSE_NEW_ENTRIES      → bot.state.is_paused = True
         - FLATTEN_BOT            → route exit orders for that bot
         - FLATTEN_TIER_B         → flatten every Tier-B bot
         - FLATTEN_ALL            → flatten everything, kill switch armed
         - FLATTEN_TIER_A_PREEMPTIVE → flatten MNQ + NQ only
    5. Dispatch events via AlertDispatcher per alerts.yaml routing.
    6. Heartbeat tick.
    7. Append a structured entry to docs/runtime_log.jsonl.

Modes
-----
--dry-run   (default True)   orders go to a MockRouter that only logs.
--live                       orders go to venues.router.SmartRouter.
--bot NAME                   only run that bot (still honors kill-switch).
--max-bars N                 stop after N ticks; useful for smoke tests / CI.
--tick-interval SECS         default 5; 0 in dry-run for fast tests.
--state-path PATH            override roadmap_state.json (tests).
--config-dir PATH            override configs/.
--log-path PATH              override docs/runtime_log.jsonl.

This script is safe by default: creds absent + --dry-run means *no* network
calls, no orders, no money. Preflight is the gate that flips --live.

Exit codes
----------
0 = clean exit (max-bars reached or Ctrl-C after clean drain)
2 = config/parse error
3 = kill-switch flatten-all triggered during run (we still exit 0 if that is
    the user-intended path; flatten-all + --dry-run exits 0).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from apex_predator.core.consistency_guard import (
    ConsistencyGuard,
    ConsistencyStatus,
    apex_trading_day_iso,
)
from apex_predator.core.kill_switch_latch import KillSwitchLatch
from apex_predator.core.kill_switch_runtime import (
    ApexEvalSnapshot,
    BotSnapshot,
    CorrelationSnapshot,
    FundingSnapshot,
    KillAction,
    KillSeverity,
    KillSwitch,
    KillVerdict,
    PortfolioSnapshot,
)
from apex_predator.core.market_quality import format_market_context_summary
from apex_predator.obs.alert_dispatcher import AlertDispatcher
from apex_predator.obs.heartbeat import HeartbeatMonitor

if TYPE_CHECKING:
    from collections.abc import Callable

    from apex_predator.core.trailing_dd_tracker import TrailingDDTracker

logger = logging.getLogger("apex_predator.runtime")

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------- #
# Mock router — only logs. Safe default when --dry-run or when no creds.
# ---------------------------------------------------------------------------- #
class MockRouter:
    """Stand-in for venues.router.SmartRouter.

    Logs every `place_with_failover` and `flatten` attempt, never touches the
    network. We use this when running with --dry-run or when required creds
    are missing. It intentionally mirrors the surface we actually call from
    the runtime so swapping in the real router is a one-line change.
    """

    name = "mock"

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._fills: list[dict[str, Any]] = []

    async def place_with_failover(self, req: Any, *_, **__) -> dict[str, Any]:
        entry = {
            "ts": time.time(),
            "kind": "place_order",
            "symbol": getattr(req, "symbol", "?"),
            "side": getattr(req, "side", "?"),
            "qty": getattr(req, "qty", 0),
            "mock": True,
        }
        self._fills.append(entry)
        self._write(entry)
        return entry

    async def flatten(self, symbol: str, reason: str) -> dict[str, Any]:
        entry = {
            "ts": time.time(),
            "kind": "flatten",
            "symbol": symbol,
            "reason": reason,
            "mock": True,
        }
        self._fills.append(entry)
        self._write(entry)
        return entry

    def _write(self, entry: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------- #
# Runtime config (loaded from configs/*.yaml + .env + roadmap_state.json)
# ---------------------------------------------------------------------------- #
@dataclass
class RuntimeConfig:
    tradovate: dict[str, Any] = field(default_factory=dict)
    bybit: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    kill_switch: dict[str, Any] = field(default_factory=dict)
    go_state: dict[str, Any] = field(default_factory=dict)
    live: bool = False
    dry_run: bool = True
    bot_filter: str | None = None
    max_bars: int = 0
    tick_interval_s: float = 5.0
    state_path: Path = field(default_factory=lambda: ROOT / "roadmap_state.json")
    config_dir: Path = field(default_factory=lambda: ROOT / "configs")
    log_path: Path = field(default_factory=lambda: ROOT / "docs" / "runtime_log.jsonl")


def _load_yaml(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def load_runtime_config(
    config_dir: Path,
    state_path: Path,
    *,
    live: bool,
    dry_run: bool,
    bot_filter: str | None,
    max_bars: int,
    tick_interval_s: float,
    log_path: Path,
) -> RuntimeConfig:
    cfg = RuntimeConfig(
        tradovate=_load_yaml(config_dir / "tradovate.yaml"),
        bybit=_load_yaml(config_dir / "bybit.yaml"),
        alerts=_load_yaml(config_dir / "alerts.yaml"),
        kill_switch=_load_yaml(config_dir / "kill_switch.yaml"),
        live=live,
        dry_run=dry_run,
        bot_filter=bot_filter,
        max_bars=max_bars,
        tick_interval_s=tick_interval_s,
        state_path=state_path,
        config_dir=config_dir,
        log_path=log_path,
    )
    if state_path.exists():
        try:
            rs = json.loads(state_path.read_text(encoding="utf-8"))
            cfg.go_state = (rs.get("shared_artifacts", {}) or {}).get("apex_go_state", {}) or {}
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("failed to parse %s: %s", state_path, exc)
    return cfg


# ---------------------------------------------------------------------------- #
# Bot registry — what can run, and under which go_state flag
# ---------------------------------------------------------------------------- #
@dataclass
class BotBinding:
    """Maps a bot name → go_state flag + factory + tier letter."""

    name: str
    tier: str                          # "A" | "B"
    flag: str                          # key in apex_go_state
    factory: Callable[[], Any]         # lazy import inside factory
    symbol: str                        # for logging + funding lookup


def _mk_mnq() -> Any:
    from apex_predator.bots.mnq.bot import MnqBot
    return MnqBot()


def _mk_nq() -> Any:
    from apex_predator.bots.nq.bot import NqBot
    return NqBot()


def _mk_crypto_seed() -> Any:
    from apex_predator.bots.crypto_seed.bot import CryptoSeedBot
    return CryptoSeedBot()


def _mk_eth_perp() -> Any:
    from apex_predator.bots.eth_perp.bot import EthPerpBot
    return EthPerpBot()


def _mk_sol_perp() -> Any:
    from apex_predator.bots.sol_perp.bot import SolPerpBot
    return SolPerpBot()


def _mk_xrp_perp() -> Any:
    from apex_predator.bots.xrp_perp.bot import XrpPerpBot
    return XrpPerpBot()


# The bindings live here (not in a YAML) because which bots exist is a
# code-level decision; only *whether they run* is configurable.
BOT_BINDINGS: list[BotBinding] = [
    BotBinding("mnq",         "A", "tier_a_mnq_live",  _mk_mnq,         "MNQ"),
    BotBinding("nq",          "A", "tier_a_nq_live",   _mk_nq,          "NQ"),
    BotBinding("crypto_seed", "B", "tier_b_testnet",   _mk_crypto_seed, "BTCUSDT"),
    BotBinding("eth_perp",    "B", "tier_b_mainnet",   _mk_eth_perp,    "ETHUSDT"),
    BotBinding("sol_perp",    "B", "tier_b_mainnet",   _mk_sol_perp,    "SOLUSDT"),
    BotBinding("xrp_perp",    "B", "tier_b_mainnet",   _mk_xrp_perp,    "XRPUSDT"),
]


def select_active_bots(
    bindings: list[BotBinding],
    go_state: dict[str, Any],
    bot_filter: str | None,
) -> list[BotBinding]:
    """Return the subset that are (a) flagged live and (b) match --bot filter.

    Honors the global kill flag: if kill_switch_active=True, nothing runs.
    """
    if bool(go_state.get("kill_switch_active", False)):
        return []
    out: list[BotBinding] = []
    for b in bindings:
        if bot_filter is not None and b.name != bot_filter:
            continue
        if bool(go_state.get(b.flag, False)):
            out.append(b)
    return out


# ---------------------------------------------------------------------------- #
# Snapshots
# ---------------------------------------------------------------------------- #
def build_bot_snapshot(binding: BotBinding, bot: Any) -> BotSnapshot:
    st = bot.state
    runtime_snapshot = getattr(bot, "runtime_snapshot", None)
    market_context_summary = None
    market_context_summary_text = None
    if isinstance(runtime_snapshot, dict):
        summary = runtime_snapshot.get("market_context_summary")
        if isinstance(summary, dict) and summary:
            market_context_summary = summary
            market_context_summary_text = runtime_snapshot.get("market_context_summary_text")
            if not isinstance(market_context_summary_text, str) or not market_context_summary_text.strip():
                market_context_summary_text = format_market_context_summary(summary)
    return BotSnapshot(
        name=binding.name,
        tier=binding.tier,
        equity_usd=float(st.equity),
        peak_equity_usd=float(st.peak_equity),
        session_realized_pnl_usd=float(getattr(st, "todays_pnl", 0.0)),
        consecutive_losses=int(getattr(st, "consecutive_losses", 0) or 0),
        open_position_count=int(len(getattr(st, "open_positions", []) or [])),
        market_context_summary=market_context_summary,
        market_context_summary_text=market_context_summary_text,
    )


def build_portfolio_snapshot(snapshots: list[BotSnapshot]) -> PortfolioSnapshot:
    total = sum(s.equity_usd for s in snapshots)
    peak = sum(s.peak_equity_usd for s in snapshots)
    daily_pnl = sum(s.session_realized_pnl_usd for s in snapshots)
    return PortfolioSnapshot(
        total_equity_usd=float(total),
        peak_equity_usd=float(peak),
        daily_realized_pnl_usd=float(daily_pnl),
    )


def build_apex_eval_snapshot(cfg: RuntimeConfig, snapshots: list[BotSnapshot]) -> ApexEvalSnapshot:
    """Crude proxy: distance_to_limit = (peak - current) vs configured trailing DD.

    The real Apex API lives in venues.tradovate; when creds are wired this
    will be replaced by a live read. For now we compute from aggregate tier_a
    equity.
    """
    ta = [s for s in snapshots if s.tier == "A"]
    if not ta:
        return ApexEvalSnapshot(trailing_dd_limit_usd=2500.0, distance_to_limit_usd=2500.0)
    current = sum(s.equity_usd for s in ta)
    peak = sum(s.peak_equity_usd for s in ta)
    trailing_dd = float(
        (cfg.tradovate.get("apex_eval", {}) or {}).get("trailing_drawdown_usd", 2500.0)
    )
    dd = max(0.0, peak - current)
    distance = max(0.0, trailing_dd - dd)
    return ApexEvalSnapshot(trailing_dd_limit_usd=trailing_dd, distance_to_limit_usd=distance)


def build_funding_snapshot(cfg: RuntimeConfig) -> FundingSnapshot:
    """Placeholder — real feed lives in venues.bybit.get_funding().

    We return empty bps dict; KillSwitch then emits no funding verdicts.
    """
    _ = cfg
    return FundingSnapshot(symbol_to_bps={})


def build_correlation_snapshot(cfg: RuntimeConfig) -> CorrelationSnapshot:
    """Placeholder — real rolling corr lives in features.correlations."""
    _ = cfg
    return CorrelationSnapshot(window_minutes=60, pair_abs_corr={})


# ---------------------------------------------------------------------------- #
# Verdict handlers — the "act on it" side
# ---------------------------------------------------------------------------- #
@dataclass
class ActionReport:
    """What the runtime actually did in response to a verdict this tick."""

    verdict: KillVerdict
    executed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def apply_verdict(
    verdict: KillVerdict,
    active: list[tuple[BotBinding, Any]],
    router: Any,
    dispatcher: AlertDispatcher,
) -> ActionReport:
    report = ActionReport(verdict=verdict)
    act = verdict.action

    if act is KillAction.CONTINUE:
        return report

    if act is KillAction.HALVE_SIZE:
        sym = verdict.evidence.get("symbol")
        for b, bot in active:
            if sym and b.symbol != sym:
                continue
            before = bot.config.risk_per_trade_pct
            bot.config.risk_per_trade_pct = max(0.1, before / 2.0)
            report.executed.append(f"halved risk on {b.name}: {before}% -> {bot.config.risk_per_trade_pct}%")
        dispatcher.send("circuit_trip", {"verdict": verdict.__dict__})
        return report

    if act is KillAction.PAUSE_NEW_ENTRIES:
        for b, bot in active:
            bot.state.is_paused = True
            report.executed.append(f"paused entries on {b.name}")
        dispatcher.send("circuit_trip", {"verdict": verdict.__dict__})
        return report

    if act is KillAction.FLATTEN_BOT:
        scope_bot = verdict.scope.removeprefix("bot:")
        for b, bot in active:
            # allow scope="bot:<name>" OR "bot:<symbol>" (funding uses symbol)
            if scope_bot not in (b.name, b.symbol):
                continue
            bot.state.is_paused = True
            await _flatten_bot(b, router, verdict.reason)
            report.executed.append(f"flattened {b.name}")
        dispatcher.send("kill_switch", {"verdict": verdict.__dict__})
        return report

    if act is KillAction.FLATTEN_TIER_B:
        for b, bot in active:
            if b.tier != "B":
                continue
            bot.state.is_paused = True
            await _flatten_bot(b, router, verdict.reason)
            report.executed.append(f"flattened {b.name} (tier_b)")
        dispatcher.send("kill_switch", {"verdict": verdict.__dict__})
        return report

    if act is KillAction.FLATTEN_TIER_A_PREEMPTIVE:
        for b, bot in active:
            if b.tier != "A":
                continue
            bot.state.is_paused = True
            await _flatten_bot(b, router, verdict.reason)
            report.executed.append(f"flattened {b.name} (tier_a preempt)")
        dispatcher.send("apex_preempt", {"verdict": verdict.__dict__})
        return report

    if act is KillAction.FLATTEN_ALL:
        for b, bot in active:
            bot.state.is_killed = True
            bot.state.is_paused = True
            await _flatten_bot(b, router, verdict.reason)
            report.executed.append(f"flattened {b.name} (global)")
        dispatcher.send("kill_switch", {"verdict": verdict.__dict__, "scope": "global"})
        return report

    report.errors.append(f"unhandled action: {act}")
    return report


async def _flatten_bot(binding: BotBinding, router: Any, reason: str) -> None:
    """Best-effort flatten via whatever router surface is wired."""
    try:
        if hasattr(router, "flatten"):
            await router.flatten(binding.symbol, reason)
        else:
            logger.warning("router has no flatten(); skipping real action for %s", binding.name)
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("flatten failed for %s: %s", binding.name, exc)


# ---------------------------------------------------------------------------- #
# Bar generator — wired to your live feed in production; stub for dry-run
# ---------------------------------------------------------------------------- #
def _synthetic_bar(symbol: str, i: int) -> dict[str, Any]:
    """Zero-info bar so dry-run smoke tests don't explode.

    Real feed lives in data_pipeline / venues.*; dry-run only needs *a* bar to
    exercise the loop.
    """
    return {
        "symbol": symbol,
        "open": 100.0 + i * 0.01,
        "high": 100.5 + i * 0.01,
        "low":  99.5 + i * 0.01,
        "close": 100.25 + i * 0.01,
        "volume": 1000,
        "avg_volume": 1000,
        "orb_high": 0.0,
        "orb_low":  0.0,
        "ema_21": 100.0,
        "adx_14": 22.0,
        "atr_14": 1.0,
        "vwap": 100.0,
    }


# ---------------------------------------------------------------------------- #
# Main runtime loop
# ---------------------------------------------------------------------------- #
class ApexRuntime:
    """Self-contained runtime. Construct, call run(). Safe by default."""

    def __init__(
        self,
        cfg: RuntimeConfig,
        *,
        kill_switch: KillSwitch | None = None,
        kill_switch_latch: KillSwitchLatch | None = None,
        trailing_dd_tracker: TrailingDDTracker | None = None,
        consistency_guard: ConsistencyGuard | None = None,
        dispatcher: AlertDispatcher | None = None,
        router: Any | None = None,
        heartbeat: HeartbeatMonitor | None = None,
        bindings: list[BotBinding] | None = None,
    ) -> None:
        self.cfg = cfg
        self.kill_switch = kill_switch or KillSwitch(cfg.kill_switch or {})
        # Disk-backed latch: survives process crash so FLATTEN_ALL /
        # FLATTEN_TIER_A_PREEMPTIVE / FLATTEN_TIER_B trips refuse re-boot
        # until an operator runs clear_kill_switch.
        self.kill_switch_latch = kill_switch_latch or KillSwitchLatch(
            ROOT / "state" / "kill_switch_latch.json",
        )
        # Optional tick-granular trailing-DD tracker. When present, the
        # runtime feeds tier-A aggregate equity into it each loop and
        # uses its ApexEvalSnapshot instead of the bar-level proxy from
        # build_apex_eval_snapshot(). Defaults to None (no override)
        # because the tracker requires a durable state file and the
        # operator must choose where that lives (likely ROOT/state/).
        self.trailing_dd_tracker = trailing_dd_tracker

        # LIVE-MODE GATE (blocker #2, risk-advocate D3 review).
        # The legacy build_apex_eval_snapshot() fallback does NOT implement
        # the Apex trailing-DD freeze rule (peak >= start + cap => floor
        # locks at start). Running LIVE without the tick-precise tracker
        # therefore risks a silent eval bust when equity retraces past the
        # unfrozen floor. Fail-closed: refuse construction so the operator
        # cannot boot a live session with missing D2 wiring.
        if cfg.live and not cfg.dry_run and trailing_dd_tracker is None:
            msg = (
                "ApexRuntime: live mode requires a TrailingDDTracker "
                "(cfg.live=True, cfg.dry_run=False, trailing_dd_tracker=None). "
                "The legacy bar-level proxy does not implement the Apex "
                "freeze rule and will silently mis-price the floor once "
                "peak >= start + trailing_dd. Wire a tracker via "
                "trailing_dd_tracker=TrailingDDTracker.load_or_init(...). "
                "See core/trailing_dd_tracker.py."
            )
            raise RuntimeError(msg)
        # Optional Apex 30% consistency-rule guard. When present, the
        # runtime records today's realized tier-A PnL each loop and logs
        # a WARN alert if the largest winning day climbs into the
        # [warning, threshold) band, or a CRITICAL alert on VIOLATION.
        # The guard is advisory (does not force-flatten); it lets the
        # operator spot concentration risk early enough to stop pushing
        # on a very-green day.
        self.consistency_guard = consistency_guard
        self._last_consistency_status: ConsistencyStatus | None = None
        self.dispatcher = dispatcher or AlertDispatcher(
            cfg.alerts or {},
            log_path=ROOT / "docs" / "alerts_log.jsonl",
        )
        self.heartbeat = heartbeat or HeartbeatMonitor()
        self.bindings = bindings if bindings is not None else BOT_BINDINGS
        self._stop = asyncio.Event()

        # Router selection: --live AND creds present → real. Else mock.
        if router is not None:
            self.router = router
        elif cfg.live and _tradovate_creds_present() and not cfg.dry_run:
            self.router = _build_real_router(cfg)
        else:
            self.router = MockRouter(log_path=cfg.log_path.with_name("runtime_mock_orders.jsonl"))

    # -- public entrypoint -------------------------------------------------- #
    async def run(self) -> int:
        # --- Boot gate: refuse to start if a catastrophic verdict ever
        # tripped the latch in a prior run. Clear with
        #   python -m apex_predator.scripts.clear_kill_switch \
        #          --confirm --operator <your_name>
        # This runs BEFORE we instantiate bots or touch the router so a
        # TRIPPED latch can never place orders. ----------------------- #
        boot_ok, boot_reason = self.kill_switch_latch.boot_allowed()
        if not boot_ok:
            logger.critical("apex runtime boot REFUSED: %s", boot_reason)
            self.dispatcher.send("boot_refused", {"reason": boot_reason})
            self._log(kind="boot_refused", meta={"reason": boot_reason})
            return 3

        bots_instantiated = self._instantiate_active_bots()
        self._log(kind="runtime_start", meta={
            "mode": "live" if (self.cfg.live and not self.cfg.dry_run) else "dry_run",
            "active_bots": [b.name for b, _ in bots_instantiated],
            "go_state": self.cfg.go_state,
        })
        self.dispatcher.send("runtime_start", {
            "active_bots": [b.name for b, _ in bots_instantiated],
            "live": self.cfg.live and not self.cfg.dry_run,
        })

        if not bots_instantiated:
            self._log(kind="no_active_bots", meta={"go_state": self.cfg.go_state})
            return 0

        # Boot hooks
        for b, bot in bots_instantiated:
            self.heartbeat.register(b.name, timeout_s=120)
            try:
                await bot.start()
            except Exception as exc:  # pragma: no cover — defensive
                logger.error("bot.start failed for %s: %s", b.name, exc)

        exit_code = 0
        bar_i = 0
        try:
            while not self._stop.is_set():
                if self.cfg.max_bars and bar_i >= self.cfg.max_bars:
                    break
                await self._tick(bots_instantiated, bar_i)
                bar_i += 1
                if self.cfg.tick_interval_s > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.tick_interval_s)
        finally:
            for b, bot in bots_instantiated:
                try:
                    await bot.stop()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.error("bot.stop failed for %s: %s", b.name, exc)
                self.heartbeat.deregister(b.name)
            self.dispatcher.send("runtime_stop", {"bars": bar_i})
            self._log(kind="runtime_stop", meta={"bars": bar_i})
        return exit_code

    def request_stop(self) -> None:
        self._stop.set()

    # -- single-tick --------------------------------------------------------- #
    async def _tick(self, bots: list[tuple[BotBinding, Any]], bar_i: int) -> None:
        # 1. re-read go_state (user may have typed KILL APEX NOW between ticks)
        self._refresh_go_state()
        if bool(self.cfg.go_state.get("kill_switch_active", False)):
            for b, bot in bots:
                bot.state.is_paused = True
                bot.state.is_killed = True
                await _flatten_bot(b, self.router, "operator kill phrase")
            self.dispatcher.send("kill_switch", {"reason": "operator kill phrase"})
            self._log(kind="operator_kill", meta={"bar": bar_i})
            self.request_stop()
            return

        # 2. feed each bot a bar (stub if no live feed)
        for b, bot in bots:
            bar = _synthetic_bar(b.symbol, bar_i)
            try:
                await bot.on_bar(bar)
            except Exception as exc:  # pragma: no cover — defensive
                logger.error("on_bar failed for %s: %s", b.name, exc)
                self.dispatcher.send("bot_error", {"bot": b.name, "err": str(exc)})
            self.heartbeat.tick(b.name)

        # 3. snapshot + kill-switch evaluate
        snapshots = [build_bot_snapshot(b, bot) for b, bot in bots]
        portfolio = build_portfolio_snapshot(snapshots)
        # Tick-granular trailing-DD path: if a tracker is attached,
        # feed the current tier-A aggregate equity into it and use its
        # snapshot as the authoritative apex_eval for this tick. The
        # tracker persists peak + frozen flag to disk and applies the
        # Apex freeze rule (peak >= start + cap => floor locks). The
        # fall-through branch preserves the legacy bar-level proxy so
        # a runtime without a tracker still emits a plausible snapshot.
        if self.trailing_dd_tracker is not None:
            ta_equity = sum(s.equity_usd for s in snapshots if s.tier == "A")
            apex_eval = self.trailing_dd_tracker.update(
                current_equity_usd=float(ta_equity),
            )
        else:
            apex_eval = build_apex_eval_snapshot(self.cfg, snapshots)
        funding = build_funding_snapshot(self.cfg)
        correlations = build_correlation_snapshot(self.cfg)
        verdicts = self.kill_switch.evaluate(
            bots=snapshots,
            portfolio=portfolio,
            correlations=correlations,
            funding=funding,
            apex_eval=apex_eval,
        )

        # 4. act on each verdict
        reports: list[ActionReport] = []
        for v in verdicts:
            # Persist before we act. record_verdict is idempotent and
            # first-trip-wins: only FLATTEN_ALL / FLATTEN_TIER_A_PREEMPTIVE
            # / FLATTEN_TIER_B flip the latch. If the write somehow fails
            # we still want to try the live flatten below, so we log and
            # keep going rather than swallowing the verdict.
            try:
                latched = self.kill_switch_latch.record_verdict(v)
                if latched:
                    self.dispatcher.send(
                        "kill_switch_latched",
                        {
                            "action": v.action.value,
                            "scope": v.scope,
                            "reason": v.reason,
                        },
                    )
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "kill_switch_latch.record_verdict failed "
                    "(action=%s scope=%s): %s",
                    v.action.value, v.scope, exc,
                )

            rep = await apply_verdict(v, bots, self.router, self.dispatcher)
            reports.append(rep)
            if v.action is KillAction.FLATTEN_ALL:
                self.request_stop()

        # 4b. feed consistency guard with today's tier-A realized PnL.
        # Emits a status-transition alert when the largest-winning-day
        # ratio climbs into the WARNING or VIOLATION band. On VIOLATION
        # we ALSO synthesize a KillVerdict(PAUSE_NEW_ENTRIES) so the
        # verdict-dispatch path flips every tier-A bot's is_paused flag.
        # That takes the guard from "log-only" to "pause + alert" --
        # closing the risk-advocate's D3 gap without force-flattening
        # (existing positions are allowed to finish; only new entries are
        # blocked until the operator investigates).
        #
        # Uses the Apex session-day helper (17:00 CT rollover, DST-aware)
        # -- NOT UTC midnight. UTC midnight splits a US equity-futures
        # session across two day buckets, which understates the real
        # "largest day" and inflates the denominator.
        if self.consistency_guard is not None:
            today = apex_trading_day_iso()
            today_ta_pnl = float(
                sum(
                    s.session_realized_pnl_usd
                    for s in snapshots
                    if s.tier == "A"
                )
            )
            try:
                verdict = self.consistency_guard.record_intraday(
                    today, today_ta_pnl,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "consistency_guard.record_intraday failed: %s", exc,
                )
            else:
                prior = self._last_consistency_status
                self._last_consistency_status = verdict.status
                is_transition = (
                    verdict.status in (
                        ConsistencyStatus.WARNING,
                        ConsistencyStatus.VIOLATION,
                    )
                    and verdict.status != prior
                )
                # Alert on transitions INTO WARNING/VIOLATION, not every
                # tick while we're already there -- the log already
                # captures the steady-state condition.
                if is_transition:
                    self.dispatcher.send(
                        "consistency_status",
                        {
                            "status": verdict.status.value,
                            "largest_day_usd": verdict.largest_day_usd,
                            "largest_day_date": verdict.largest_day_date,
                            "largest_day_ratio": verdict.largest_day_ratio,
                            "total_net_profit_usd": (
                                verdict.total_net_profit_usd
                            ),
                            "max_allowed_day_usd": (
                                verdict.max_allowed_day_usd
                            ),
                            "headroom_today_usd": verdict.headroom_today_usd,
                            "today_pnl_usd": verdict.today_pnl_usd,
                        },
                    )
                    self._log(kind="consistency_status", meta={
                        "status": verdict.status.value,
                        "today_pnl_usd": verdict.today_pnl_usd,
                        "largest_day_usd": verdict.largest_day_usd,
                        "ratio": verdict.largest_day_ratio,
                    })

                # VIOLATION enforcement: synthesize a PAUSE_NEW_ENTRIES
                # verdict so every tier-A bot flips is_paused. Fire on
                # each tick while in VIOLATION (idempotent -- is_paused
                # is already True, but a re-fire is harmless and keeps
                # the log visible). We intentionally DO NOT flatten;
                # existing positions can close normally. The operator
                # must clear the guard history (close the eval bucket,
                # trim the largest day off via new trades, or reset)
                # before new entries resume.
                if verdict.status is ConsistencyStatus.VIOLATION:
                    pause_verdict = KillVerdict(
                        action=KillAction.PAUSE_NEW_ENTRIES,
                        severity=KillSeverity.CRITICAL,
                        reason=(
                            f"apex 30% consistency VIOLATION: largest day "
                            f"{verdict.largest_day_usd:.2f} ({verdict.largest_day_date}) "
                            f"is {verdict.largest_day_ratio:.1%} of total net profit "
                            f"{verdict.total_net_profit_usd:.2f} "
                            f"(cap {verdict.threshold_pct:.0%})"
                        ),
                        scope="tier_a",
                        evidence={
                            "largest_day_usd": verdict.largest_day_usd,
                            "largest_day_date": verdict.largest_day_date,
                            "largest_day_ratio": verdict.largest_day_ratio,
                            "total_net_profit_usd": (
                                verdict.total_net_profit_usd
                            ),
                            "threshold_pct": verdict.threshold_pct,
                        },
                    )
                    try:
                        pause_rep = await apply_verdict(
                            pause_verdict, bots, self.router, self.dispatcher,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.error(
                            "consistency_guard PAUSE_NEW_ENTRIES dispatch "
                            "failed: %s", exc,
                        )
                    else:
                        reports.append(pause_rep)
                        verdicts.append(pause_verdict)

        # 5. structured per-tick log
        self._log(kind="tick", meta={
            "bar": bar_i,
            "active": [b.name for b, _ in bots],
            "verdicts": [
                {
                    "action": v.action.value,
                    "severity": v.severity.value,
                    "scope": v.scope,
                    "reason": v.reason,
                }
                for v in verdicts
            ],
            "executed": [e for rep in reports for e in rep.executed],
            "errors":   [e for rep in reports for e in rep.errors],
        })

    # -- helpers ------------------------------------------------------------- #
    def _instantiate_active_bots(self) -> list[tuple[BotBinding, Any]]:
        active = select_active_bots(self.bindings, self.cfg.go_state, self.cfg.bot_filter)
        out: list[tuple[BotBinding, Any]] = []
        for b in active:
            try:
                bot = b.factory()
            except Exception as exc:  # pragma: no cover — defensive
                logger.error("factory failed for %s: %s", b.name, exc)
                continue
            out.append((b, bot))
        return out

    def _refresh_go_state(self) -> None:
        if not self.cfg.state_path.exists():
            return
        try:
            rs = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
            self.cfg.go_state = (rs.get("shared_artifacts", {}) or {}).get("apex_go_state", {}) or {}
        except Exception:  # pragma: no cover — defensive
            pass

    def _log(self, *, kind: str, meta: dict[str, Any]) -> None:
        entry = {"ts": time.time(), "kind": kind, **meta}
        self.cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------- #
# Real router wiring — only called when --live AND creds present
# ---------------------------------------------------------------------------- #
def _tradovate_creds_present() -> bool:
    keys = ("TRADOVATE_CLIENT_ID", "TRADOVATE_CLIENT_SECRET", "TRADOVATE_USERNAME", "TRADOVATE_PASSWORD")
    return all(bool(os.environ.get(k)) for k in keys)


def _build_real_router(cfg: RuntimeConfig) -> Any:
    """Build the real SmartRouter. Isolated so dry-run never imports live deps."""
    from apex_predator.venues.router import SmartRouter
    _ = cfg  # reserved for per-venue config in future
    return SmartRouter()


# ---------------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="APEX PREDATOR live runtime loop")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="No orders — mock router only (default True)")
    ap.add_argument("--live", action="store_true",
                    help="Flip off --dry-run and use the real router. Requires creds.")
    ap.add_argument("--bot", dest="bot", default=None,
                    help="Only run this bot name (mnq|nq|crypto_seed|eth_perp|sol_perp|xrp_perp)")
    ap.add_argument("--max-bars", type=int, default=0,
                    help="Stop after N ticks (0 = forever). Used by CI smoke-tests.")
    ap.add_argument("--tick-interval", type=float, default=5.0,
                    help="Seconds between ticks (0 = tight loop for tests)")
    ap.add_argument("--state-path", type=Path, default=ROOT / "roadmap_state.json")
    ap.add_argument("--config-dir", type=Path, default=ROOT / "configs")
    ap.add_argument("--log-path", type=Path, default=ROOT / "docs" / "runtime_log.jsonl")
    return ap.parse_args(argv)


def _install_signal_handlers(runtime: ApexRuntime) -> None:
    def _handler(signum, _frame) -> None:  # noqa: ANN001
        logger.info("signal %s received — draining", signum)
        runtime.request_stop()
    try:
        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handler)
    except Exception:  # pragma: no cover — Windows edge cases
        pass


async def _amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # --live overrides --dry-run
    dry_run = not args.live

    cfg = load_runtime_config(
        config_dir=args.config_dir,
        state_path=args.state_path,
        live=args.live,
        dry_run=dry_run,
        bot_filter=args.bot,
        max_bars=args.max_bars,
        tick_interval_s=args.tick_interval,
        log_path=args.log_path,
    )

    runtime = ApexRuntime(cfg)
    _install_signal_handlers(runtime)

    print("APEX PREDATOR  -- runtime")
    print("=" * 64)
    print(f"mode          : {'LIVE' if (cfg.live and not cfg.dry_run) else 'DRY-RUN'}")
    print(f"router        : {type(runtime.router).__name__}")
    print(f"max_bars      : {cfg.max_bars or '∞'}")
    print(f"tick_interval : {cfg.tick_interval_s}s")
    print(f"go_state      : {cfg.go_state or '(empty)'}")
    print(f"bot_filter    : {cfg.bot_filter or '(all)'}")
    print("=" * 64)

    rc = await runtime.run()
    return rc


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_eta_live
========================================
The runtime loop that ties every piece together.

Reads
-----
* configs/<active_futures_venue>.yaml for each venue in
  ``venues.router.ACTIVE_FUTURES_VENUES`` (currently ibkr.yaml +
  tastytrade.yaml; tradovate.yaml is DORMANT and not required),
  configs/bybit.yaml, configs/kill_switch.yaml, configs/alerts.yaml
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
--tick-interval SECS         default 1.0 (was 5.0 pre-R2); 0 in dry-run for fast tests.
                             Live mode is validated against kill_switch cushion.
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
from eta_engine.core.broker_equity_adapter import (
    BrokerEquityAdapter,
    BrokerEquityNotAvailableError,
    NullBrokerEquityAdapter,
    SafeBrokerEquityAdapter,
    make_poller_for,
)
from eta_engine.core.broker_equity_reconciler import BrokerEquityReconciler
from eta_engine.core.consistency_guard import (
    ConsistencyGuard,
    ConsistencyStatus,
    apex_trading_day_iso_cme,
)
from eta_engine.core.kill_switch_latch import KillSwitchLatch
from eta_engine.core.kill_switch_runtime import (
    ApexEvalSnapshot,
    BotSnapshot,
    CorrelationSnapshot,
    FundingSnapshot,
    KillAction,
    KillSeverity,
    KillSwitch,
    KillVerdict,
    PortfolioSnapshot,
    validate_apex_tick_cadence,
)
from eta_engine.core.market_quality import format_market_context_summary
from eta_engine.core.runtime_log_rotator import RuntimeLogRotator
from eta_engine.obs.alert_dispatcher import AlertDispatcher
from eta_engine.obs.heartbeat import HeartbeatMonitor

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.core.broker_equity_poller import BrokerEquityPoller
    from eta_engine.core.trailing_dd_tracker import TrailingDDTracker

logger = logging.getLogger("eta_engine.runtime")

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
    # tradovate config slot is preserved through the broker dormancy
    # mandate -- the adapter still ships and the apex_eval block (read
    # by build_apex_eval_snapshot) lives here. When Tradovate is in
    # DORMANT_BROKERS, configs/tradovate.yaml may be absent;
    # _load_yaml returns {} and downstream cfg.tradovate.get(...)
    # calls fall through to defaults.
    tradovate: dict[str, Any] = field(default_factory=dict)
    bybit: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    kill_switch: dict[str, Any] = field(default_factory=dict)
    go_state: dict[str, Any] = field(default_factory=dict)
    live: bool = False
    dry_run: bool = True
    bot_filter: str | None = None
    max_bars: int = 0
    # R2 closure: default lowered from 5.0s -> 1.0s to match Apex's sub-second
    # DD enforcement window. A 5s tick could silently cross Apex's floor
    # mid-interval on a fast move; 1s keeps at least one tick per second
    # inside the cushion. Paper/backtest runs can override downward freely
    # (tick_interval_s=0.0 == as-fast-as-possible); live mode is validated
    # against the cushion via validate_apex_tick_cadence().
    tick_interval_s: float = 1.0
    state_path: Path = field(default_factory=lambda: ROOT / "roadmap_state.json")
    config_dir: Path = field(default_factory=lambda: ROOT / "configs")
    log_path: Path = field(default_factory=lambda: ROOT / "docs" / "runtime_log.jsonl")
    # B3 closure (v0.1.69): operator-supplied expected size of the
    # Apex eval account in USD. When set, the tier-A aggregate-equity
    # invariant validator checks ``sum(tier_a.equity_usd)`` against
    # [undersize * size, oversize * size] each tick (default oversize
    # 1.5x flags the canonical config-bug case where two tier-A bots
    # each track the full account size). Leave None to skip the
    # bounded check (the negative-aggregate / non-finite checks
    # still fire). Threaded through ``load_runtime_config`` so the
    # operator can set it via a future ``--account-size-usd`` flag
    # or ``configs/eta_account.yaml`` -- both tracked as v0.1.70+
    # ergonomics work; v0.1.69 ships the validator + runtime hook.
    tier_a_account_size_usd: float | None = None
    # User-selectable aggressiveness preset (conservative / balanced /
    # aggressive). Drives every position-sizing and circuit-breaker knob
    # from a single choice; see core/risk_profile.py for exact values.
    # ``None`` means "use the default" (balanced) when the runtime
    # eventually consumes this — the field is plumbed here so the
    # private-portal can pass per-user selections through without
    # touching argparse internals.
    risk_profile_name: str | None = None


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
    risk_profile_name: str | None = None,
) -> RuntimeConfig:
    # _load_yaml returns {} for missing files; tradovate.yaml is
    # expected to be absent while the broker is DORMANT.
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
        risk_profile_name=risk_profile_name,
    )
    if state_path.exists():
        try:
            rs = json.loads(state_path.read_text(encoding="utf-8"))
            cfg.go_state = (rs.get("shared_artifacts", {}) or {}).get("apex_go_state", {}) or {}
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("failed to parse %s: %s", state_path, exc)
    # R2 closure: in live mode, require tick cadence to be fast enough that
    # the Apex cushion can absorb at least one worst-case tick-sized move.
    # We read the cushion out of the just-loaded kill_switch.yaml. Non-live
    # runs are exempt (paper / backtest can tolerate arbitrary cadence).
    cushion_usd = float(
        ((cfg.kill_switch.get("tier_a", {}) or {}).get("apex_eval_preemptive", {}) or {}).get("cushion_usd", 500.0),
    )
    validate_apex_tick_cadence(
        tick_interval_s=max(tick_interval_s, 1e-9),
        cushion_usd=cushion_usd,
        live=live,
    )
    return cfg


# ---------------------------------------------------------------------------- #
# Bot registry — what can run, and under which go_state flag
# ---------------------------------------------------------------------------- #
@dataclass
class BotBinding:
    """Maps a bot name → go_state flag + factory + tier letter."""

    name: str
    tier: str  # "A" | "B"
    flag: str  # key in apex_go_state
    factory: Callable[[], Any]  # lazy import inside factory
    symbol: str  # for logging + funding lookup


def _mk_mnq() -> Any:
    from eta_engine.bots.mnq.bot import MnqBot

    return MnqBot()


def _mk_nq() -> Any:
    from eta_engine.bots.nq.bot import NqBot

    return NqBot()


def _mk_crypto_seed() -> Any:
    from eta_engine.bots.crypto_seed.bot import CryptoSeedBot

    return CryptoSeedBot()


def _mk_eth_perp() -> Any:
    from eta_engine.bots.eth_perp.bot import EthPerpBot

    return EthPerpBot()


def _mk_sol_perp() -> Any:
    from eta_engine.bots.sol_perp.bot import SolPerpBot

    return SolPerpBot()


def _mk_xrp_perp() -> Any:
    from eta_engine.bots.xrp_perp.bot import XrpPerpBot

    return XrpPerpBot()


# The bindings live here (not in a YAML) because which bots exist is a
# code-level decision; only *whether they run* is configurable.
BOT_BINDINGS: list[BotBinding] = [
    BotBinding("mnq", "A", "tier_a_mnq_live", _mk_mnq, "MNQ"),
    BotBinding("nq", "A", "tier_a_nq_live", _mk_nq, "NQ"),
    BotBinding("crypto_seed", "B", "tier_b_testnet", _mk_crypto_seed, "BTCUSDT"),
    BotBinding("eth_perp", "B", "tier_b_mainnet", _mk_eth_perp, "ETHUSDT"),
    BotBinding("sol_perp", "B", "tier_b_mainnet", _mk_sol_perp, "SOLUSDT"),
    BotBinding("xrp_perp", "B", "tier_b_mainnet", _mk_xrp_perp, "XRPUSDT"),
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

    Originally written when Tradovate was the assumed Apex venue and
    its API was the canonical live read. Under the broker dormancy
    mandate (Tradovate DORMANT) the trailing-DD value is read from
    cfg.tradovate.apex_eval.trailing_drawdown_usd if that yaml is
    present, else from the v0.1.59 R3 trailing_dd_tracker (which is
    the authoritative source today). When Tradovate un-dormants, the
    venues.tradovate adapter's live API will replace this proxy --
    until then this is a bar-level fallback.
    """
    ta = [s for s in snapshots if s.tier == "A"]
    if not ta:
        return ApexEvalSnapshot(trailing_dd_limit_usd=2500.0, distance_to_limit_usd=2500.0)
    current = sum(s.equity_usd for s in ta)
    peak = sum(s.peak_equity_usd for s in ta)
    # cfg.tradovate is {} when tradovate.yaml is absent (DORMANT path);
    # the .get(..., 2500.0) fallback then yields the Apex-eval default.
    trailing_dd = float((cfg.tradovate.get("apex_eval", {}) or {}).get("trailing_drawdown_usd", 2500.0))
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
        "low": 99.5 + i * 0.01,
        "close": 100.25 + i * 0.01,
        "volume": 1000,
        "avg_volume": 1000,
        "orb_high": 0.0,
        "orb_low": 0.0,
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
        broker_equity_reconciler: BrokerEquityReconciler | None = None,
        broker_equity_poller: BrokerEquityPoller | None = None,
        runtime_log_rotator: RuntimeLogRotator | None = None,
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

        # R1 closure: observation-only broker MTM drift detection.
        # When both are wired, the runtime starts the poller in run(),
        # stops it in the finally block, and feeds each tick's tier-A
        # aggregate equity to the reconciler. Classification is logged
        # and out-of-tolerance events (broker_below_logical) fan out as
        # a `broker_equity_drift` alert. The reconciler itself is
        # observation-only -- it does NOT synthesize KillVerdicts today
        # (deferred to v0.2.x; M2 in docs/red_team_d2_d3_review.md).
        # Lands when: scripts/calibrate_broker_drift_tolerance.py emits
        # a recommendation backed by 30+ days of live-paper data, AND
        # configs/kill_switch.yaml grows a tier_a.broker_drift entry,
        # AND test_run_eta_live asserts the synthesised KillVerdict
        # fires on sustained drift. Wiring both kwargs to None preserves
        # legacy behaviour (no drift detection).
        self.broker_equity_reconciler = broker_equity_reconciler
        self.broker_equity_poller = broker_equity_poller
        # Per-tick drift status cache so we only fire an alert on the
        # transition into broker_below_logical, not every tick while
        # the drift persists. Mirrors the consistency-guard pattern.
        self._last_broker_drift_reason: str | None = None
        # H3 closure (v0.1.66): sustained-drift re-alert tracking.
        # When the reconciler stays in is_in_drift_state for longer
        # than ``broker_drift_realert_interval_s`` the runtime fires
        # a follow-up alert (kind="sustained") so the operator does
        # not silently lose visibility on a multi-hour drift event.
        # Reset to None on a clean exit transition.
        self._last_broker_drift_alert_ts: float | None = None
        self.broker_drift_realert_interval_s: float = 1800.0
        # B3 closure (v0.1.69): per-tick tier-A aggregate-equity
        # invariant verdict cache. Lets the runtime fire an alert
        # only on the transition INTO a violation, not every tick
        # while the misconfigured fleet keeps producing the same
        # bogus aggregate. Mirrors the consistency-guard / drift-
        # latch alert patterns elsewhere in this loop.
        self._last_tier_a_invariant_verdict: str | None = None

        # M3 closure (v0.1.67): runtime-log rotator. Defaults to a
        # 100 MB / 1-day-gzip / 30-day-retain policy bound to
        # ``cfg.log_path``. When passed explicitly, the operator can
        # override (e.g. a tighter retention window for an eval VM
        # with a small disk). Pass ``runtime_log_rotator=False``-y
        # equivalent (None + skip-the-default) is intentionally not
        # supported -- a runtime that writes a JSONL log with no
        # rotation is the failure mode M3 was raised to close.
        if runtime_log_rotator is None:
            runtime_log_rotator = RuntimeLogRotator(log_path=cfg.log_path)
        self.runtime_log_rotator = runtime_log_rotator
        # Rotation cadence: every Nth tick, the runtime calls
        # ``rotator.run()`` (rotate -> gzip -> prune). Default 600 ticks
        # ~= 10 min at the v0.1.65 1s tick interval. The rotator is
        # idempotent so over-calling is safe; under-calling lets the
        # log grow past the rotation threshold momentarily.
        self.runtime_log_rotate_every_n_ticks: int = 600

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
        #   python -m eta_engine.scripts.clear_kill_switch \
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
        self._log(
            kind="runtime_start",
            meta={
                "mode": "live" if (self.cfg.live and not self.cfg.dry_run) else "dry_run",
                "active_bots": [b.name for b, _ in bots_instantiated],
                "go_state": self.cfg.go_state,
            },
        )
        self.dispatcher.send(
            "runtime_start",
            {
                "active_bots": [b.name for b, _ in bots_instantiated],
                "live": self.cfg.live and not self.cfg.dry_run,
            },
        )

        if not bots_instantiated:
            self._log(kind="no_active_bots", meta={"go_state": self.cfg.go_state})
            return 0

        # 2026-04-27 risk-sage hardening: register the fleet-wide
        # FleetRiskGate BEFORE starting any bot. The bootstrap helper
        # sums starting_capital_usd across the active bot set,
        # constructs a gate sized for that fleet, registers it as the
        # process singleton (so every venue client's
        # assert_fleet_within_budget() call sees it), and attaches it
        # to each bot so closing fills feed the running aggregate.
        # Failure to register is non-fatal — the gate becomes a no-op
        # rather than blocking startup, but the operator dashboard
        # will surface the missing aggregator. See
        # safety/fleet_bootstrap.py for the contract.
        try:
            from eta_engine.safety.fleet_bootstrap import bootstrap_fleet_risk
            fleet_bots = [bot for _b, bot in bots_instantiated]
            bootstrap_fleet_risk(fleet_bots)
            self._log(
                kind="fleet_risk_gate_registered",
                meta={"n_bots": len(fleet_bots)},
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.error("bootstrap_fleet_risk failed: %s", exc)

        # Boot hooks
        for b, bot in bots_instantiated:
            self.heartbeat.register(b.name, timeout_s=120)
            try:
                await bot.start()
            except Exception as exc:  # pragma: no cover — defensive
                logger.error("bot.start failed for %s: %s", b.name, exc)

        # R1 closure: start the broker-equity poller (if wired). The
        # poller runs an async refresh loop in the background; _tick
        # reads the cached snapshot via reconciler.reconcile(). We
        # start AFTER bot.start so we never spin up a network pinger
        # for a runtime that is about to abort on bot-factory errors,
        # and stop FIRST in the finally block (before bot.stop) so a
        # slow broker-logout can't delay the draining of live bots.
        if self.broker_equity_poller is not None:
            try:
                await self.broker_equity_poller.start()
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "broker_equity_poller.start failed: %s",
                    exc,
                )

        exit_code = 0
        bar_i = 0
        try:
            while not self._stop.is_set():
                if self.cfg.max_bars and bar_i >= self.cfg.max_bars:
                    break
                await self._tick(bots_instantiated, bar_i)
                # M3 closure (v0.1.67): rotate / gzip / prune the
                # runtime log on a coarse cadence. Idempotent so
                # over-calling is safe; running every Nth tick keeps
                # the cost amortised (~10 min between checks at the
                # default 1s tick cadence). Errors degrade to a log
                # warning; the eval keeps running.
                if (
                    bar_i > 0
                    and self.runtime_log_rotate_every_n_ticks > 0
                    and (bar_i % self.runtime_log_rotate_every_n_ticks) == 0
                ):
                    try:
                        outcome = self.runtime_log_rotator.run()
                    except Exception as exc:  # pragma: no cover -- defensive
                        logger.warning(
                            "runtime_log_rotator.run failed: %s",
                            exc,
                        )
                    else:
                        if any(outcome.values()):
                            self._log(
                                kind="log_rotation",
                                meta={
                                    "rotated": [str(p) for p in outcome["rotated"]],
                                    "gzipped": [str(p) for p in outcome["gzipped"]],
                                    "pruned": [str(p) for p in outcome["pruned"]],
                                },
                            )
                bar_i += 1
                if self.cfg.tick_interval_s > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.tick_interval_s)
        finally:
            # Stop the broker-equity poller FIRST -- its refresh loop
            # holds an aiohttp/broker-SDK session and should be drained
            # before we cancel the bot tasks to keep the shutdown order
            # clean (reconciler never reads a poller that no longer has
            # a live session). Guarded so a None poller (legacy path)
            # is a no-op.
            if self.broker_equity_poller is not None:
                try:
                    await self.broker_equity_poller.stop()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.error(
                        "broker_equity_poller.stop failed: %s",
                        exc,
                    )
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
        # Compute tier-A aggregate equity ONCE -- it is consumed by
        # both the trailing-DD tracker (to synthesize the apex_eval
        # snapshot) and the broker-equity reconciler (as the "logical"
        # side of the drift comparison). Keeping a single source of
        # truth avoids a rounding divergence between the two paths.
        ta_equity = float(sum(s.equity_usd for s in snapshots if s.tier == "A"))
        # B3 closure (v0.1.69): tier-A aggregate-equity invariant. The
        # reconciler / tracker both consume ``ta_equity`` as the
        # logical-equity side of comparisons. If two tier-A bots are
        # mistakenly each tracking the full account size, ``ta_equity``
        # over-states the actual account by 2x and the reconciler
        # mistakes it for $50K of broker_below_logical drift. The
        # validator catches the obvious config-bug shape and fires an
        # advisory alert; strict promotion to KillVerdict is v0.2.x.
        # Once-per-session: we cache the last verdict so we only fire
        # an alert on the transition into a violation, not every tick.
        from eta_engine.core.eta_account_invariant import (
            validate_tier_a_aggregate_equity,
        )

        invariant_result = validate_tier_a_aggregate_equity(
            snapshots=snapshots,
            expected_account_size_usd=getattr(
                self.cfg,
                "tier_a_account_size_usd",
                None,
            ),
        )
        prior_inv_verdict = self._last_tier_a_invariant_verdict
        self._last_tier_a_invariant_verdict = invariant_result.verdict
        if not invariant_result.ok and (prior_inv_verdict != invariant_result.verdict):
            logger.warning(
                "tier_a_invariant: %s",
                invariant_result.reason,
            )
            self.dispatcher.send(
                "tier_a_invariant_violation",
                invariant_result.as_dict(),
            )
        # Tick-granular trailing-DD path: if a tracker is attached,
        # feed the current tier-A aggregate equity into it and use its
        # snapshot as the authoritative apex_eval for this tick. The
        # tracker persists peak + frozen flag to disk and applies the
        # Apex freeze rule (peak >= start + cap => floor locks). The
        # fall-through branch preserves the legacy bar-level proxy so
        # a runtime without a tracker still emits a plausible snapshot.
        if self.trailing_dd_tracker is not None:
            apex_eval = self.trailing_dd_tracker.update(
                current_equity_usd=ta_equity,
            )
        else:
            apex_eval = build_apex_eval_snapshot(self.cfg, snapshots)

        # R1 closure: observation-only broker MTM drift check. The
        # reconciler reads the latest broker-side net-liq from the
        # poller's TTL cache (or no-data if the poller hasn't had a
        # successful refresh yet), classifies the gap vs the logical
        # tier-A aggregate, and records stats. We fan out an alert
        # only on the TRANSITION into `broker_below_logical` so a
        # sustained drift does not spam the alert channel. Other
        # classifications (within_tolerance / broker_above_logical /
        # no_broker_data) are logged via _log(kind="tick") below.
        rec_reason: str | None = None
        rec_drift_usd: float | None = None
        rec_drift_pct: float | None = None
        rec_in_tol: bool | None = None
        if self.broker_equity_reconciler is not None:
            try:
                rec_result = self.broker_equity_reconciler.reconcile(
                    logical_equity_usd=ta_equity,
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "broker_equity_reconciler.reconcile failed: %s",
                    exc,
                )
            else:
                rec_reason = rec_result.reason
                rec_drift_usd = rec_result.drift_usd
                rec_drift_pct = rec_result.drift_pct_of_logical
                rec_in_tol = rec_result.in_tolerance
                self._last_broker_drift_reason = rec_reason
                # H3 closure (v0.1.66): the reconciler now exposes a
                # latched drift state with hysteresis. The runtime fires
                # three kinds of broker_equity_drift alerts:
                #
                #   transition  -- ReconcileResult.transition == "entered_drift"
                #   sustained   -- still in drift, last alert was
                #                  >= broker_drift_realert_interval_s ago
                #   recovered   -- ReconcileResult.transition == "exited_drift"
                #
                # within_tolerance / no_broker_data / broker_above_logical
                # ticks are silent on the alert channel (still logged).
                now_mono = time.monotonic()
                drift_kind: str | None = None
                if rec_result.transition == "entered_drift":
                    drift_kind = "transition"
                    self._last_broker_drift_alert_ts = now_mono
                elif rec_result.transition == "exited_drift":
                    drift_kind = "recovered"
                    self._last_broker_drift_alert_ts = None
                elif rec_result.is_in_drift_state:
                    last_ts = self._last_broker_drift_alert_ts
                    if last_ts is not None and (now_mono - last_ts >= self.broker_drift_realert_interval_s):
                        drift_kind = "sustained"
                        self._last_broker_drift_alert_ts = now_mono
                if drift_kind is not None:
                    self.dispatcher.send(
                        "broker_equity_drift",
                        {
                            "kind": drift_kind,
                            "reason": rec_reason,
                            "is_in_drift_state": rec_result.is_in_drift_state,
                            "transition": rec_result.transition,
                            "logical_equity_usd": ta_equity,
                            "broker_equity_usd": rec_result.broker_equity_usd,
                            "drift_usd": rec_drift_usd,
                            "drift_pct_of_logical": rec_drift_pct,
                        },
                    )
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
                    "kill_switch_latch.record_verdict failed (action=%s scope=%s): %s",
                    v.action.value,
                    v.scope,
                    exc,
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
        # Uses the CME-calendar-aware Apex session-day helper (17:00 CT
        # rollover + DST-aware + weekend/holiday roll-forward). Apex has no
        # activity on CME-closed days (weekends, US federal closures), so
        # the bucket rolls forward to the next real trading day -- matching
        # Apex's own accounting. The earlier v0.1.58 fix handled the
        # 17:00-CT rollover only; R4 closure in v0.1.59 adds the calendar.
        if self.consistency_guard is not None:
            today = apex_trading_day_iso_cme()
            today_ta_pnl = float(sum(s.session_realized_pnl_usd for s in snapshots if s.tier == "A"))
            try:
                verdict = self.consistency_guard.record_intraday(
                    today,
                    today_ta_pnl,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "consistency_guard.record_intraday failed: %s",
                    exc,
                )
            else:
                prior = self._last_consistency_status
                self._last_consistency_status = verdict.status
                is_transition = (
                    verdict.status
                    in (
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
                            "total_net_profit_usd": (verdict.total_net_profit_usd),
                            "max_allowed_day_usd": (verdict.max_allowed_day_usd),
                            "headroom_today_usd": verdict.headroom_today_usd,
                            "today_pnl_usd": verdict.today_pnl_usd,
                        },
                    )
                    self._log(
                        kind="consistency_status",
                        meta={
                            "status": verdict.status.value,
                            "today_pnl_usd": verdict.today_pnl_usd,
                            "largest_day_usd": verdict.largest_day_usd,
                            "ratio": verdict.largest_day_ratio,
                        },
                    )

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
                            "total_net_profit_usd": (verdict.total_net_profit_usd),
                            "threshold_pct": verdict.threshold_pct,
                        },
                    )
                    try:
                        pause_rep = await apply_verdict(
                            pause_verdict,
                            bots,
                            self.router,
                            self.dispatcher,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.error(
                            "consistency_guard PAUSE_NEW_ENTRIES dispatch failed: %s",
                            exc,
                        )
                    else:
                        reports.append(pause_rep)
                        verdicts.append(pause_verdict)

        # 5. structured per-tick log
        tick_meta: dict[str, Any] = {
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
            "errors": [e for rep in reports for e in rep.errors],
        }
        # R1 closure: surface the broker-drift classification in the
        # tick log whenever the reconciler is wired. Keeps the alert
        # channel quiet (only transitions fire there) while every
        # tick's classification lands in runtime_log.jsonl for the
        # post-session audit trail.
        if self.broker_equity_reconciler is not None:
            tick_meta["broker_equity"] = {
                "reason": rec_reason,
                "in_tolerance": rec_in_tol,
                "drift_usd": rec_drift_usd,
                "drift_pct_of_logical": rec_drift_pct,
            }
        self._log(kind="tick", meta=tick_meta)

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
    from eta_engine.venues.router import SmartRouter

    _ = cfg  # reserved for per-venue config in future
    return SmartRouter()


# ---------------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="EVOLUTIONARY TRADING ALGO live runtime loop")
    ap.add_argument("--dry-run", action="store_true", default=True, help="No orders — mock router only (default True)")
    ap.add_argument("--live", action="store_true", help="Flip off --dry-run and use the real router. Requires creds.")
    ap.add_argument(
        "--bot", dest="bot", default=None, help="Only run this bot name (mnq|nq|crypto_seed|eth_perp|sol_perp|xrp_perp)"
    )
    ap.add_argument("--max-bars", type=int, default=0, help="Stop after N ticks (0 = forever). Used by CI smoke-tests.")
    ap.add_argument(
        "--tick-interval",
        type=float,
        default=1.0,
        help=(
            "Seconds between ticks (0 = tight loop for tests; default 1.0 post-R2 to match Apex sub-second DD window)"
        ),
    )
    ap.add_argument("--state-path", type=Path, default=ROOT / "roadmap_state.json")
    ap.add_argument("--config-dir", type=Path, default=ROOT / "configs")
    ap.add_argument("--log-path", type=Path, default=ROOT / "docs" / "runtime_log.jsonl")
    ap.add_argument(
        "--require-firm-health",
        choices=["off", "advisory", "strict"],
        default=None,
        help=(
            "Run firm_health probes before booting. "
            "'off' = skip; 'advisory' = log probe results, never block; "
            "'strict' = REFUSE TO BOOT unless verdict is READY. "
            "Default: 'strict' under --live, 'advisory' under --dry-run."
        ),
    )
    ap.add_argument(
        "--risk-profile",
        choices=["conservative", "balanced", "aggressive"],
        default=None,
        help=(
            "User-selectable aggressiveness preset. Drives every "
            "position-sizing and circuit-breaker knob from a single "
            "choice (see core/risk_profile.py for exact values). "
            "Default: 'balanced' — matches the published methodology "
            "and the back-tested track record."
        ),
    )
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


def _build_broker_equity_adapter(
    *,
    live: bool,
    dry_run: bool,
    allow_live_no_drift: bool = False,
) -> BrokerEquityAdapter:
    """Pick the broker-equity adapter for this run.

    R1 closure (B1, v0.1.64): production CLI was constructing
    ``ApexRuntime(cfg)`` with no broker-equity reconciler/poller, so
    the entire R1 stack was dormant code in live mode -- exactly the
    silent-drift exposure R1 was meant to close. This helper picks the
    right adapter for the mode and is wired into ``_amain`` so every
    boot path (dry-run, paper, live) instantiates SOMETHING.

    H6 closure (v0.1.65): live mode with no real broker source now
    refuses to boot unless the operator explicitly opts in via
    ``APEX_ALLOW_LIVE_NO_DRIFT=1`` (caught in ``_amain`` and threaded
    through as ``allow_live_no_drift``). The previous v0.1.64
    behaviour silently fell through to ``NullBrokerEquityAdapter`` --
    a busy operator scanning startup output could miss the WARN line
    and run a whole eval with drift detection off. Refusing to boot
    is loud; opting in is explicit.

    Resolution
    ----------
    * **Live mode** (``cfg.live and not cfg.dry_run``): try IBKR
      (primary per ``memory/broker_dormancy_mandate.md``); if creds
      are missing, fall through to Tastytrade; if both are missing,
      raise :class:`BrokerEquityNotAvailableError` UNLESS
      ``allow_live_no_drift=True`` (operator opt-in via env var), in
      which case degrade to :class:`NullBrokerEquityAdapter` with a
      LOUD WARN.
    * **Dry-run / paper**: always :class:`NullBrokerEquityAdapter`.
      This is intentional -- it keeps the runtime wire-up exercised
      end-to-end (so a regression in B1 surfaces in the paper smoke
      test) while ensuring no broker calls go out in dry-run.

    Tradovate is not consulted (DORMANT per the broker dormancy
    mandate, 2026-04-24). Wire it in here when funding clears.

    Adapters are returned wrapped in :class:`SafeBrokerEquityAdapter`
    so the "MUST NOT raise" Protocol guarantee is enforced at the
    wrapper level rather than relying on each venue's exception
    discipline (H7 closure, v0.1.65).
    """
    if not (live and not dry_run):
        return SafeBrokerEquityAdapter(NullBrokerEquityAdapter(name="paper-null"))
    # Live mode -- try IBKR first.
    try:
        from eta_engine.venues.ibkr import IbkrClientPortalVenue

        ibkr = IbkrClientPortalVenue()
        if ibkr.has_credentials():
            return SafeBrokerEquityAdapter(ibkr)
        logger.warning(
            "broker_equity: IBKR creds missing -- falling through to Tastytrade",
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("broker_equity: IBKR adapter ctor failed (%s)", exc)
    # IBKR not available -- try Tastytrade as fallback.
    try:
        from eta_engine.venues.tastytrade import TastytradeVenue

        tasty = TastytradeVenue()
        if tasty.has_credentials():
            return SafeBrokerEquityAdapter(tasty)
        logger.warning(
            "broker_equity: Tastytrade creds also missing",
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("broker_equity: Tastytrade adapter ctor failed (%s)", exc)
    # No real broker source available in live mode. H6 closure: refuse
    # to boot unless the operator has explicitly opted in.
    if not allow_live_no_drift:
        msg = (
            "live mode requested but no real broker equity source is "
            "available (IBKR + Tastytrade creds both missing). Refusing "
            "to boot -- drift detection would be silently disabled. "
            "Fix: populate IBKR_* / TASTYTRADE_* env vars, OR opt in "
            "explicitly via APEX_ALLOW_LIVE_NO_DRIFT=1 to acknowledge "
            "that the eval will run with drift detection OFF."
        )
        raise BrokerEquityNotAvailableError(msg)
    logger.warning(
        "broker_equity: APEX_ALLOW_LIVE_NO_DRIFT=1 -- live eval will "
        "run with drift detection OFF (no_broker_data each tick)",
    )
    return SafeBrokerEquityAdapter(
        NullBrokerEquityAdapter(name="live-null-no-creds"),
    )


async def _amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # --live overrides --dry-run
    dry_run = not args.live

    # firm_health gate (#20). Default policy: live -> strict, dry-run -> advisory.
    # Operator can override either way via --require-firm-health=off.
    fh_mode = args.require_firm_health or ("strict" if args.live else "advisory")
    if fh_mode != "off":
        try:
            from eta_engine.scripts import firm_health as _fh
        except ImportError as e:
            logger.warning("firm_health unavailable, skipping gate: %s", e)
        else:
            results = _fh.run_all(skip_bridge=False)
            verdict = _fh.verdict_from(results, strict=False)
            print(f"firm_health   : verdict={verdict} mode={fh_mode}")
            if fh_mode == "strict" and verdict != "READY":
                bad = [r for r in results if r.status in ("fail", "warn", "skip_broken")]
                logger.error(
                    "firm_health gate REFUSED boot (verdict=%s, mode=strict). Issues:\n%s",
                    verdict,
                    "\n".join(f"  [{r.status}] {r.name}: {r.detail}" for r in bad),
                )
                logger.error(
                    "Override with `--require-firm-health off` (NOT recommended for live) or fix the failing probes."
                )
                return 4

    cfg = load_runtime_config(
        config_dir=args.config_dir,
        state_path=args.state_path,
        live=args.live,
        dry_run=dry_run,
        bot_filter=args.bot,
        max_bars=args.max_bars,
        tick_interval_s=args.tick_interval,
        log_path=args.log_path,
        risk_profile_name=args.risk_profile,
    )

    # R1 closure (B1, v0.1.64): wire the broker-equity reconciler /
    # poller end-to-end. Picks the right adapter for the mode (real
    # venue in live, NullBrokerEquityAdapter in dry-run / paper).
    # See ``_build_broker_equity_adapter`` for the resolution chain.
    # H6 closure (v0.1.65): live + no creds = refuse-to-boot unless
    # the operator explicitly opts in via APEX_ALLOW_LIVE_NO_DRIFT=1.
    allow_live_no_drift = os.environ.get("APEX_ALLOW_LIVE_NO_DRIFT") == "1"
    try:
        broker_adapter = _build_broker_equity_adapter(
            live=cfg.live,
            dry_run=cfg.dry_run,
            allow_live_no_drift=allow_live_no_drift,
        )
    except BrokerEquityNotAvailableError as exc:
        logger.error("boot refused: %s", exc)
        print(f"BOOT REFUSED: {exc}")
        return 78  # EX_CONFIG -- operator config error
    # H4 partial (v0.1.69): warn when net-liq is unchanged for 12
    # consecutive polls (= 60 seconds at the 5s refresh). Active
    # market hours typically tick MTM at least once a minute even
    # for unfunded paper accounts; sustained zero-change is a
    # plausible signal that the broker is serving a stale snapshot.
    # Quiet markets / overnight WILL trip this; the warn is single-
    # fire so it cannot log-spam, and observation-only -- it never
    # demotes the cached value to no_broker_data.
    broker_poller = make_poller_for(
        broker_adapter,
        refresh_s=5.0,
        identical_warn_after=12,
    )
    # H2 closure (Red Team v0.1.64 review, shipped v0.1.66):
    # asymmetric tolerances. broker_below_logical is the dangerous
    # direction (cushion over-stated, eval-bust risk) so we use a
    # tight threshold there. broker_above_logical is benign (MTM lag
    # / dividend / rebate) and using a tight threshold here would
    # generate alert spam on every fast-market IBKR snapshot delay.
    # Defaults chosen for the Apex 50K eval; operator can tune via
    # configs/kill_switch.yaml in v0.1.67+.
    broker_reconciler = BrokerEquityReconciler(
        broker_equity_source=broker_poller.current,
        tolerance_below_usd=20.0,  # tight: $20 ~ 1 MNQ tick of cushion
        tolerance_below_pct=0.0005,  # tight: 0.05% of $50K = $25
        tolerance_above_usd=200.0,  # loose: 4x below, anti-spam
        tolerance_above_pct=0.005,  # loose: 0.5% of $50K = $250
    )

    # R3 + D2 closure (v0.1.65 wave 2): wire ConsistencyGuard and
    # TrailingDDTracker in production. The roadmap-vs-code reconciler
    # (`scripts/_audit_roadmap_vs_code.py`) flagged R3 as WIRED-FAIL
    # because _amain was constructing ApexRuntime with no
    # `consistency_guard=` / `trailing_dd_tracker=` kwargs -- exactly
    # the same B1-shaped wire-up gap the Red Team caught for R1. The
    # runtime had a hard `RuntimeError` raise on live + missing
    # trailing_dd_tracker (so live mode was hard-broken); the
    # consistency guard was silently no-op without the kwarg.
    #
    # State files are scoped to ``cfg.state_path.parent / "state"``
    # rather than ``ROOT / "state"`` so smoke tests (which pass
    # ``--state-path tmp_path/s.json``) get isolated state and do not
    # pollute the operator's real ``state/`` directory between runs.
    # In production with the default ``--state-path roadmap_state.json``,
    # this resolves to ``ROOT / "state"`` -- same path as before.
    from eta_engine.core.trailing_dd_tracker import TrailingDDTracker

    state_dir = cfg.state_path.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    consistency_guard = ConsistencyGuard.load_or_init(
        state_dir / "consistency_guard.json",
    )
    # Apex 50K eval defaults: $50,000 starting balance, $2,500 trailing
    # DD cap. These match configs/kill_switch.yaml::tier_a.apex_eval_preemptive
    # ($500 cushion before the $2,500 floor). For dry-run / paper the
    # values are nominal -- the tracker still observes the cushion
    # regardless of whether the equity numbers feed real PnL.
    trailing_dd_tracker = TrailingDDTracker.load_or_init(
        state_dir / "trailing_dd_tracker.json",
        starting_balance_usd=50_000.0,
        trailing_dd_cap_usd=2_500.0,
    )
    # Scope the KillSwitchLatch to the same isolated state dir so the
    # tracker's verdicts persist alongside the rest of the per-run
    # state. Without this override the latch defaults to
    # ``ROOT / "state" / "kill_switch_latch.json"`` and a smoke test
    # that trips it (e.g. by ticking with empty go_state) leaks the
    # tripped latch into the operator's working tree.
    kill_switch_latch = KillSwitchLatch(state_dir / "kill_switch_latch.json")

    runtime = ApexRuntime(
        cfg,
        broker_equity_reconciler=broker_reconciler,
        broker_equity_poller=broker_poller,
        consistency_guard=consistency_guard,
        trailing_dd_tracker=trailing_dd_tracker,
        kill_switch_latch=kill_switch_latch,
    )
    _install_signal_handlers(runtime)

    print("EVOLUTIONARY TRADING ALGO  -- runtime")
    print("=" * 64)
    print(f"mode          : {'LIVE' if (cfg.live and not cfg.dry_run) else 'DRY-RUN'}")
    print(f"router        : {type(runtime.router).__name__}")
    print(f"max_bars      : {cfg.max_bars or '∞'}")
    print(f"tick_interval : {cfg.tick_interval_s}s")
    print(f"go_state      : {cfg.go_state or '(empty)'}")
    print(f"bot_filter    : {cfg.bot_filter or '(all)'}")
    print(
        f"broker_equity : {broker_adapter.name} "
        f"(below tol=${broker_reconciler.tolerance_below_usd:.0f}/"
        f"{broker_reconciler.tolerance_below_pct:.4%}, "
        f"above tol=${broker_reconciler.tolerance_above_usd:.0f}/"
        f"{broker_reconciler.tolerance_above_pct:.3%}, "
        f"refresh_s=5.0)",
    )
    print(
        f"consistency   : 30%-rule guard wired (state={(state_dir / 'consistency_guard.json').name})",
    )
    _tdd_state = trailing_dd_tracker.state()
    print(
        f"trailing_dd   : {_tdd_state.starting_balance_usd:.0f} "
        f"start / {_tdd_state.trailing_dd_cap_usd:.0f} cap "
        f"(floor={trailing_dd_tracker.floor_usd():.0f})",
    )
    # User-selectable risk profile. The bot doesn't yet read these values
    # to override its internal sizing — that integration ships with the
    # private portal. Banner surfaces the choice so dry-run smoke tests
    # can confirm the CLI flag plumbed through ``RuntimeConfig``.
    _rp_name = getattr(cfg, "risk_profile_name", None) or "balanced (default)"
    print(f"risk_profile  : {_rp_name}")
    # B3 boot banner: tier-A account-size invariant. Prints either
    # an explicit size + bounds or 'none (unbounded)' so the operator
    # sees at startup whether the validator's bounded checks are on.
    _ta_acct_size = getattr(cfg, "tier_a_account_size_usd", None)
    if _ta_acct_size is None:
        print(
            "tier_a_invar  : aggregate-equity validator advisory (size unset; only negative/non-finite checks fire)",
        )
    else:
        print(
            f"tier_a_invar  : aggregate-equity validator advisory "
            f"(size=${_ta_acct_size:.0f}, oversize=1.5x, undersize=0x)",
        )
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

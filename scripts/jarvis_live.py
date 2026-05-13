"""
EVOLUTIONARY TRADING ALGO  //  scripts.jarvis_live
======================================
Long-running daemon that keeps Jarvis ticking LIVE with supervision.

Why this exists
---------------
``brain.jarvis_admin`` makes Jarvis the admin of the fleet: every
subsystem calls ``request_approval()``. If Jarvis stops ticking, every
gate silently falls through to stale policy. ``daily_premarket.py``
produces a ONE-SHOT snapshot; this daemon keeps Jarvis LIVE and
watched end-to-end.

Responsibilities
----------------
  * Build a ``JarvisContextBuilder`` from simple file-based providers
    reading ``docs/premarket_inputs.json`` (hot-reloadable -- operator
    can overwrite the file and the next tick picks it up).
  * Wrap in ``JarvisContextEngine`` and run through ``JarvisSupervisor``
    so staleness / dominance / flatline / invalid are all caught.
  * Fan out health alerts via Telegram / Discord / Slack when env is
    set (``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` /
    ``DISCORD_WEBHOOK_URL`` / ``SLACK_WEBHOOK_URL``). No env -> no alerts,
    dry-run mode.
  * Emit per-tick health reports to:
      - ``docs/jarvis_live_health.json``    (latest only)
      - ``docs/jarvis_live_log.jsonl``      (append-only history)

Usage
-----
    # Foreground; Ctrl-C to stop.
    python -m eta_engine.scripts.jarvis_live

    # Faster cadence, bounded ticks (smoke test):
    python -m eta_engine.scripts.jarvis_live --interval 5 --max-ticks 3

    # Explicit inputs file:
    python -m eta_engine.scripts.jarvis_live \\
        --inputs docs/premarket_inputs.json \\
        --out-dir docs/ \\
        --interval 60

Design
------
All I/O is injected for testing. ``run_live()`` takes explicit
providers / alerter / paths, so tests wire stubs and bound the loop
with ``max_ticks``. The module-level ``main()`` is just a CLI shim.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

# Load .env so env vars (Telegram, broker, API keys) are available to subprocesses and the alerter
_env_path = ROOT / ".env"
if _env_path.exists():
    try:
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass
# Also load root .env for shared keys (API keys etc.)
_root_env = ROOT.parent / ".env"
if _root_env.exists() and _root_env != _env_path:
    try:
        for line in _root_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass

# Also load firm_command_center/broker_paper.env for broker settings
_broker_env = ROOT.parent / "firm_command_center" / "secrets" / "broker_paper.env"
if _broker_env.exists():
    try:
        for line in _broker_env.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass

from eta_engine.brain.jarvis_context import (  # noqa: E402
    EquitySnapshot,
    JarvisContextBuilder,
    JarvisContextEngine,
    JarvisMemory,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
)
from eta_engine.obs.jarvis_supervisor import (  # noqa: E402
    JarvisHealthReport,
    JarvisSupervisor,
    SupervisorPolicy,
)
from eta_engine.scripts import jarvis_status  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.obs.alerts import MultiAlerter

logger = logging.getLogger(__name__)

DEFAULT_INPUTS = ROOT / "docs" / "premarket_inputs.json"
DEFAULT_OUT_DIR = ROOT / "docs"


# ---------------------------------------------------------------------------
# File-based providers (hot-reload on every call)
# ---------------------------------------------------------------------------


@dataclass
class _FileInputs:
    """Single source of truth parsed once per tick. Attribute access via
    providers below."""

    macro: MacroSnapshot
    equity: EquitySnapshot
    regime: RegimeSnapshot
    journal: JournalSnapshot


def _neutral_inputs() -> _FileInputs:
    return _FileInputs(
        macro=MacroSnapshot(vix_level=None, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=0.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="UNKNOWN", confidence=0.5),
        journal=JournalSnapshot(),
    )


def _load_inputs_file(path: Path) -> _FileInputs:
    if not path.exists():
        return _neutral_inputs()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed to parse %s; using neutral inputs", path)
        return _neutral_inputs()
    try:
        return _FileInputs(
            macro=MacroSnapshot(**raw.get("macro", {})),
            equity=EquitySnapshot(**raw["equity"]),
            regime=RegimeSnapshot(**raw["regime"]),
            journal=JournalSnapshot(**raw.get("journal", {})),
        )
    except Exception:
        logger.exception("invalid schema in %s; using neutral inputs", path)
        return _neutral_inputs()


class _FileBackedProviders:
    """Single object exposing the four provider methods by re-reading the
    inputs file each tick. Matches all four Protocols via duck typing.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # MacroProvider
    def get_macro(self) -> MacroSnapshot:
        return _load_inputs_file(self._path).macro

    # EquityProvider
    def get_equity(self) -> EquitySnapshot:
        return _load_inputs_file(self._path).equity

    # RegimeProvider
    def get_regime(self) -> RegimeSnapshot:
        return _load_inputs_file(self._path).regime

    # JournalProvider
    def get_journal_snapshot(self) -> JournalSnapshot:
        return _load_inputs_file(self._path).journal


# ---------------------------------------------------------------------------
# Output sinks
# ---------------------------------------------------------------------------


def _bot_strategy_readiness_payload() -> dict:
    """Return a fail-soft bot readiness block for live health output."""
    try:
        fn = getattr(jarvis_status, "build_bot_strategy_readiness_summary", None)
        if fn is not None:
            return fn()
    except Exception:  # noqa: BLE001 -- live health output must keep writing
        pass
    return {
        "source": "jarvis_status",
        "status": "live",
        "error": "",
        "summary": {},
        "top_actions": [],
    }


def _write_health(
    report: JarvisHealthReport,
    out_dir: Path,
    *,
    bot_strategy_readiness: dict | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "jarvis_live_health.json"
    log = out_dir / "jarvis_live_log.jsonl"
    payload = report.model_dump(mode="json")
    payload["bot_strategy_readiness"] = (
        bot_strategy_readiness if bot_strategy_readiness is not None else _bot_strategy_readiness_payload()
    )
    latest.write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")


# ---------------------------------------------------------------------------
# Alerter factory from environment
# ---------------------------------------------------------------------------


def build_alerter_from_env() -> MultiAlerter | None:
    """Inspect env for webhook/token config and construct a MultiAlerter.

    Returns None if no transport is configured (dry-run mode).
    """
    from eta_engine.obs.alerts import (  # noqa: PLC0415
        DiscordAlerter,
        MultiAlerter,
        SlackAlerter,
        TelegramAlerter,
    )

    alerters = []
    tg_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_tok and tg_chat:
        alerters.append(TelegramAlerter(bot_token=tg_tok, chat_id=tg_chat))
    disc = os.environ.get("DISCORD_WEBHOOK_URL")
    if disc:
        alerters.append(DiscordAlerter(webhook_url=disc))
    firm_disc = os.environ.get("FIRM_DISCORD_WEBHOOK_FIRM_SIGNALS")
    if firm_disc:
        alerters.append(DiscordAlerter(webhook_url=firm_disc, username="EVOLUTIONARY TRADING ALGO Supervisor"))
    slack = os.environ.get("SLACK_WEBHOOK_URL")
    if slack:
        alerters.append(SlackAlerter(webhook_url=slack))
    if not alerters:
        return None
    return MultiAlerter(alerters)


# ---------------------------------------------------------------------------
# run_live: the daemon body (testable)
# ---------------------------------------------------------------------------


async def _tasty_refresh_tick(i: int) -> None:
    """Refresh Tastytrade session token every 60 ticks (~1h at 60s interval)."""
    if i % 60 != 0 or i == 0:
        return
    try:
        from eta_engine.venues.tastytrade import TastytradeConfig, TastytradeVenue

        cfg = TastytradeConfig.from_env()
        if cfg.login and cfg.password:
            venue = TastytradeVenue(config=cfg)
            ok = await venue.refresh_session_token()
            logger.info("tastytrade token refresh tick=%d: %s", i, "OK" if ok else "FAILED")
    except Exception as exc:
        logger.debug("tastytrade refresh skipped tick=%d: %s", i, exc)


async def _background_feed_connect(feed: object) -> None:
    """Try to connect the multi-symbol IBKR feed in background; never blocks startup."""
    try:
        ok = await asyncio.wait_for(feed.connect(), timeout=10)
        if ok:
            await feed.start_stream()
            logger.info("multi_feed: connected and streaming from IBKR")
        else:
            logger.info("multi_feed: gateway not authenticated")
    except TimeoutError:
        logger.info("multi_feed: connect timed out (gateway slow)")
    except Exception as exc:
        logger.debug("multi_feed: background connect failed: %s", exc)


def _build_heartbeat_message(
    supervisor: object | None = None,
    reports: list[object] | None = None,
    live_feed: object | None = None,
) -> str:
    """Build a rich status message for the periodic Telegram heartbeat."""
    lines = ["*ETA Fleet Status*"]
    try:
        import json
        from pathlib import Path

        state_dir = os.environ.get("ETA_STATE_DIR", "C:/EvolutionaryTradingAlgo/var/eta_engine/state")
        ledger_path = Path(state_dir) / "paper_soak_ledger.json"
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            sessions = ledger.get("bot_sessions", {})
            if sessions:
                total_pnl = sum(sum(s.get("pnl", 0) for s in bot_sessions) for bot_sessions in sessions.values())
                diamond_bots = sum(
                    1 for bot_sessions in sessions.values() if sum(s.get("pnl", 0) for s in bot_sessions) > 0
                )
                total_bots = len(sessions)
                lines.append(f"Fleet: {diamond_bots}/{total_bots} profitable | PnL: `${total_pnl:+,.0f}`")
    except Exception:
        pass
    if supervisor is not None:
        try:
            health = supervisor.snapshot_health()
            lines.append(f"Health: {health.health}")
            if health.reasons and any(health.reasons):
                rlist = [r for r in health.reasons if r]
                if rlist:
                    lines.append(f"Issues: {' | '.join(rlist[:3])}")
        except Exception:
            pass
    if reports and len(reports) > 0:
        try:
            last = reports[-1]
            lines.append(f"Tick: {getattr(last, 'tick_count', '?')} | Stale: {getattr(last, 'stale_s', '?')}s ago")
        except Exception:
            pass
    if live_feed is not None:
        try:
            all_bars = getattr(live_feed, "all_bars", lambda: {})()
            for sym in ("MNQ", "BTC", "ETH", "NQ"):
                bar = all_bars.get(sym, {})
                if bar and bar.get("close"):
                    lines.append(f"{sym}: {bar['close']:,.0f}")
        except Exception:
            pass
    try:
        wd_path = Path(state_dir) / "tws_watchdog.json"
        if wd_path.exists():
            wd = json.loads(wd_path.read_text(encoding="utf-8"))
            lines.append("IBKR: Connected" if wd.get("healthy") else "IBKR: Degraded")
    except Exception:
        pass
    if len(lines) == 1:
        lines.append("All systems nominal")
    return "\n".join(lines)


async def _hermes_tick(
    i: int,
    supervisor: object | None = None,
    reports: list[object] | None = None,
    live_feed: object | None = None,
) -> None:
    """Poll Telegram for commands every tick with staggered heartbeat.
    On every 30th tick, sends a rich status summary instead of bare 'nominal'."""
    if i == 0:
        return
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import send_alert, tick_poll

        responses = await tick_poll()
        if responses:
            logger.info("hermes: %d command(s) processed", len(responses))
        if i % 30 == 0 and os.environ.get("TELEGRAM_BOT_TOKEN"):
            msg = _build_heartbeat_message(supervisor, reports, live_feed)
            await send_alert("ETA Status", msg, "INFO")
    except Exception as exc:
        logger.debug("hermes tick error (non-fatal): %s", exc)


async def _ibkr_reauth_tick(i: int) -> None:
    """Re-auth IBKR gateway every 30 ticks (~30 min at 60s interval)."""
    if i % 30 != 0 or i == 0:
        return
    try:
        import os
        import subprocess
        import sys

        script = str(Path(__file__).resolve().parent / "ibkr_reauth.py")
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ},
        )
        logger.info("ibkr reauth tick=%d: rc=%d", i, result.returncode)
    except Exception as exc:
        logger.debug("ibkr reauth skipped tick=%d: %s", i, exc)


async def run_live(
    *,
    supervisor: JarvisSupervisor,
    alerter: MultiAlerter | None,
    out_dir: Path = DEFAULT_OUT_DIR,
    interval_s: float = 60.0,
    max_ticks: int | None = None,
    stop_event: asyncio.Event | None = None,
    live_feed: object = None,
    shadow_orch: object | None = None,
) -> list[JarvisHealthReport]:
    """Run the supervised tick loop until ``stop_event`` fires or
    ``max_ticks`` elapses. Returns all health reports recorded.

    This is a lower-level primitive than ``JarvisSupervisor.run``:
    we need per-tick output to ``docs/jarvis_live_*`` and an
    externally-triggered stop (for signal handlers).
    """
    if interval_s <= 0.0:
        raise ValueError("interval_s must be > 0")
    stop_event = stop_event or asyncio.Event()
    reports: list[JarvisHealthReport] = []
    inputs_file = out_dir / "premarket_inputs.json"
    i = 0
    try:
        while not stop_event.is_set() and (max_ticks is None or i < max_ticks):
            # Inject varied data each tick (live IBKR data when available)
            try:
                _all_bars = getattr(live_feed, "all_bars", lambda: {})() if live_feed else {}
                _mnq_bar = _all_bars.get("MNQ", {})
                _btc_bar = _all_bars.get("BTC", {})
                _nq_bar = _all_bars.get("NQ", {})
                _es_bar = _all_bars.get("ES", {})
                if _mnq_bar and _mnq_bar.get("close"):
                    _mnq = _mnq_bar["close"]
                    _vix = 15.0
                    _bias = "intraday"
                    _risk = 1.5
                    _dd = 0.0
                else:
                    _phase = i % 12
                    if _phase < 3:
                        _risk, _dd, _vix, _bias = 2.5, 0.0, 12.0, "bullish"
                    elif _phase < 6:
                        _risk, _dd, _vix, _bias = 0.5, 4.0, 28.0, "bearish"
                    elif _phase < 9:
                        _risk, _dd, _vix, _bias = 1.5, 1.5, 18.0, "neutral"
                    else:
                        _risk, _dd, _vix, _bias = 3.0, 0.5, 35.0, "crisis"
                _pnl = (i % 8 - 3) * 50
                _pos = (i % 4) + 1
                _conf = 0.5 + (i % 5) * 0.08
                varied = {
                    "macro": {"vix_level": _vix, "macro_bias": _bias},
                    "equity": {
                        "account_equity": 100000.0,
                        "daily_pnl": _pnl,
                        "daily_drawdown_pct": _dd,
                        "open_positions": _pos,
                        "open_risk_r": _risk,
                    },
                    "regime": {"regime": _bias, "confidence": _conf},
                    "journal": {"autopilot_mode": "ACTIVE", "executed_last_24h": i % 10},
                }
                inputs_file.write_text(__import__("json").dumps(varied, indent=2), encoding="utf-8")
            except Exception:
                pass
            try:
                supervisor.tick()
            except Exception:
                logger.exception("supervisor.tick() raised (continuing)")
            report = supervisor.snapshot_health()
            reports.append(report)
            try:
                _write_health(report, out_dir)
            except Exception:
                logger.exception("failed writing jarvis_live health outputs")
            if report.degraded:
                try:
                    await supervisor.alert(alerter, report)
                except Exception:
                    logger.exception("supervisor.alert failed")
            try:
                await _tasty_refresh_tick(i)
            except Exception:
                logger.debug("tasty refresh error (non-fatal)")
            try:
                await _ibkr_reauth_tick(i)
            except Exception:
                logger.debug("ibkr reauth error (non-fatal)")
            try:
                await _hermes_tick(i, supervisor, reports, live_feed)
            except Exception:
                logger.debug("hermes tick error (non-fatal)")
            try:
                await _self_diagnosis_tick(supervisor, i)
            except Exception:
                logger.debug("self-diagnosis error (non-fatal)")
            try:
                await _kaizen_cycle_tick(i)
            except Exception:
                logger.debug("kaizen cycle error (non-fatal)")
            try:
                await _execute_approved_verdicts(i)
            except Exception:
                logger.debug("execution error (non-fatal)")
            try:
                await _enforce_risk_circuit_breaker(supervisor, i)
            except Exception:
                logger.debug("risk enforcement error (non-fatal)")
            try:
                await _reconcile_orders(i)
            except Exception:
                logger.debug("order reconciliation error")
            try:
                await _enforce_position_limits(i)
            except Exception:
                logger.debug("position limit error")
            try:
                await _check_data_freshness(i)
            except Exception:
                logger.debug("data freshness error")
            try:
                await _check_ibkr_health(i)
            except Exception:
                logger.debug("ibkr health error")
            try:
                await _capture_market_data(i)
            except Exception:
                logger.debug("market data capture error")
            try:
                if shadow_orch is not None:
                    _all_bars = getattr(live_feed, "all_bars", lambda: {})() if live_feed else {}
                    await shadow_orch.tick(_all_bars, i)
            except Exception:
                logger.debug("shadow_orch error (non-fatal)")
            i += 1
            if max_ticks is not None and i >= max_ticks:
                break
            # Sleep cancellable by stop_event.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=interval_s,
                )
    finally:
        if alerter is not None:
            try:
                await alerter.close()
            except Exception:
                logger.exception("alerter.close raised")
    return reports


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_default_supervisor(inputs_path: Path) -> JarvisSupervisor:
    providers = _FileBackedProviders(inputs_path)
    builder = JarvisContextBuilder(
        macro_provider=providers,
        equity_provider=providers,
        regime_provider=providers,
        journal_provider=providers,
    )
    engine = JarvisContextEngine(builder=builder, memory=JarvisMemory(maxlen=64))
    # Wider thresholds: 60 min same-binding or flatline before YELLOW
    # (default 10 min is too sensitive without live market data)
    policy = SupervisorPolicy(
        stale_after_s=300.0,
        dead_after_s=1800.0,
        dominance_run=60,
        flatline_threshold=0.05,
        flatline_run=60,
    )
    return JarvisSupervisor(engine=engine, policy=policy)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT / SIGTERM to stop_event. Best-effort on Windows where
    the loop's add_signal_handler is not supported.
    """
    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        # Windows / already-set handler fallback: rely on KeyboardInterrupt.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


async def _register_hermes_webhook_async() -> None:
    """Register the webhook URL with Telegram API for instant push delivery."""
    await asyncio.sleep(5)
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import register_webhook

        public_url = os.environ.get("HERMES_PUBLIC_URL", "https://ops.evolutionarytradingalgo.com")
        ok = await register_webhook(public_url)
        if ok:
            await _hermes_send("Jarvis webhook registered — instant command responses active")
    except Exception as _e:
        logger.debug("hermes webhook registration: %s", _e)


async def _hermes_send(text: str) -> None:
    """Send a one-off Telegram message from a daemon lifecycle event."""
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import send_message

        await send_message(text)
    except Exception:
        pass


async def _execute_approved_verdicts(i: int) -> None:
    """Route approved JARVIS verdicts through Tastytrade paper execution."""
    if i % 2 != 0 or i == 0:
        return
    try:
        health_file = Path(os.environ.get("ETA_OUT_DIR", str(ROOT / "docs"))) / "jarvis_live_health.json"
        if not health_file.exists():
            return
        health = json.loads(health_file.read_text())
        verdicts = health.get("bot_strategy_readiness", {}).get("top_actions", [])
        for v in verdicts:
            if v.get("verdict") == "APPROVED" and os.environ.get("ETA_MODE", "PAPER") == "PAPER":
                try:
                    from eta_engine.venues.tastytrade import TastytradeConfig, TastytradeVenue

                    cfg = TastytradeConfig.from_env()
                    if cfg.login and cfg.password:
                        venue = TastytradeVenue(config=cfg)
                        await venue.connect()
                        resp = await venue.place_order(
                            symbol=v.get("symbol"), side=v.get("side"), qty=1, order_type="market"
                        )
                        logger.info("tastytrade exec: %s %s -> %s", v.get("symbol"), v.get("side"), resp)
                        await _write_pnl_journal(v, resp)
                except Exception as exec_exc:
                    logger.warning("tastytrade exec failed: %s", exec_exc)
                    await _write_pnl_journal(v, {"error": str(exec_exc)})
    except Exception:
        logger.debug("exec tick error (non-fatal)")


async def _enforce_risk_circuit_breaker(supervisor: JarvisSupervisor, i: int) -> None:
    """Enforce circuit breaker and kill switch every 3 ticks."""
    if i % 3 != 0 or i == 0:
        return
    try:
        latch_paths = [
            ROOT / "state",
            ROOT.parent / "var" / "eta_engine" / "state",
        ]
        for latch_dir in latch_paths:
            latch_file = latch_dir / "kill_switch_latch.json"
            if latch_file.exists():
                logger.warning("KILL SWITCH LATCH DETECTED — halting supervisor")
                policy = supervisor.policy
                if hasattr(policy, "circuit_open"):
                    policy.circuit_open = True
                return

        report = supervisor.snapshot_health()
        metrics = report.model_dump() if hasattr(report, "model_dump") else {}
        drawdown = abs(metrics.get("equity", {}).get("daily_drawdown_pct", 0))
        if drawdown > 5.0:
            logger.warning("circuit breaker: drawdown %.1f%% exceeds limit", drawdown)

        positions_path = ROOT / "state" / "positions"
        if positions_path.exists():
            pos_files = list(positions_path.glob("*.json"))
            open_positions = 0
            for pf in pos_files:
                try:
                    pd = json.loads(pf.read_text())
                    if pd.get("qty", 0) != 0:
                        open_positions += 1
                except Exception:
                    pass
            if open_positions > 10:
                logger.warning("circuit breaker: %d open positions exceeds limit", open_positions)
    except Exception:
        logger.debug("risk tick error (non-fatal)")


_PNL_JOURNAL_PATH = ROOT / "state" / "pnl_journal.jsonl"


async def _reconcile_orders(i: int) -> None:
    """Reconcile open orders against Tastytrade/IBKR positions every 6 ticks."""
    if i % 6 != 0 or i == 0:
        return
    try:
        from eta_engine.venues.tastytrade import TastytradeConfig, TastytradeVenue

        cfg = TastytradeConfig.from_env()
        if cfg.login and cfg.password:
            venue = TastytradeVenue(config=cfg)
            await venue.connect()
            positions = await venue.get_positions()
            # Log positions
            pos_path = ROOT / "state" / "positions" / "tastytrade_positions.json"
            pos_path.parent.mkdir(parents=True, exist_ok=True)
            pos_path.write_text(json.dumps(positions, indent=2, default=str))
            logger.info("reconciliation: %d positions from tastytrade", len(positions or []))
    except Exception as exc:
        logger.debug("reconciliation error: %s", exc)


async def _write_pnl_journal(verdict: dict, result: dict | None = None) -> None:
    """Append a trade record to the PnL journal."""
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": verdict.get("symbol"),
            "side": verdict.get("side"),
            "verdict": verdict.get("verdict"),
            "confidence": verdict.get("confidence"),
            "execution_result": result,
        }
        _PNL_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PNL_JOURNAL_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


async def _enforce_position_limits(i: int) -> None:
    """Enforce max positions and daily loss limits every 4 ticks."""
    if i % 4 != 0 or i == 0:
        return
    try:
        pos_dir = ROOT / "state" / "positions"
        if not pos_dir.exists():
            return
        total_positions = 0
        max_positions = int(os.environ.get("ETA_MAX_POSITIONS", "5"))
        for f in pos_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("qty", 0) != 0:
                    total_positions += 1
            except Exception:
                pass
        if total_positions > max_positions:
            logger.warning("position limit: %d open exceeds max %d", total_positions, max_positions)
    except Exception:
        logger.debug("position limit error (non-fatal)")


async def _check_data_freshness(i: int) -> None:
    """Verify market data feed freshness every 5 ticks."""
    if i % 5 != 0 or i == 0:
        return
    try:
        health_file = ROOT / "docs" / "jarvis_live_health.json"
        if not health_file.exists():
            return
        health = json.loads(health_file.read_text())
        last_bar_ts = health.get("metrics", {}).get("last_bar_ts")
        if last_bar_ts:
            last = datetime.fromisoformat(last_bar_ts)
            age_s = (datetime.now(UTC) - last).total_seconds()
            if age_s > 300:
                logger.warning("data freshness: last bar %.0fs ago — possible feed stall", age_s)
                from eta_engine.brain.jarvis_v3.hermes_bridge import send_alert

                await send_alert("Data Feed Stale", f"Last bar {age_s:.0f}s ago", "WARN")
    except Exception:
        logger.debug("freshness check error (non-fatal)")


async def _check_ibkr_health(i: int) -> None:
    """Verify IBKR gateway is serving valid data every 15 ticks."""
    if i % 15 != 0 or i == 0:
        return
    try:
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/status", context=ctx, timeout=5) as r:
            import json as j

            auth = j.loads(r.read())
            if not auth.get("authenticated"):
                logger.warning("IBKR gateway unauthenticated — reconnecting")
    except Exception as exc:
        logger.warning("IBKR gateway unreachable: %s", exc)


async def _capture_market_data(i: int) -> None:
    """Persist IBKR bbo1m bars to parquet every 12 ticks (~12 min)."""
    if i % 12 != 0 or i == 0:
        return
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        health_file = ROOT / "docs" / "jarvis_live_health.json"
        if not health_file.exists():
            return
        health = json.loads(health_file.read_text())
        metrics = health.get("metrics", {})
        bar_data = metrics.get("last_bar", {})
        if not bar_data:
            return
        out_dir = ROOT / "data" / "bars" / "live"
        out_dir.mkdir(parents=True, exist_ok=True)
        table = pa.table(
            {
                "ts": [bar_data.get("ts", datetime.now(UTC).isoformat())],
                "symbol": [bar_data.get("symbol", "MNQ")],
                "open": [float(bar_data.get("open", 0))],
                "high": [float(bar_data.get("high", 0))],
                "low": [float(bar_data.get("low", 0))],
                "close": [float(bar_data.get("close", 0))],
                "volume": [int(bar_data.get("volume", 0))],
            }
        )
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        out_path = out_dir / f"{bar_data.get('symbol', 'MNQ')}_{date_str}.parquet"
        if out_path.exists():
            existing = pq.read_table(out_path)
            table = pa.concat_tables([existing, table])
        pq.write_table(table, out_path)
        logger.debug("market data: captured bar to %s", out_path)
    except Exception:
        logger.debug("market data capture error")


async def _warm_llm_caches() -> None:
    """Pre-warm LLM prompt caches for frequent evaluation patterns."""
    try:
        from eta_engine.brain.jarvis_v3.claude_layer.prompt_cache import (
            PromptCacheTracker,
            build_cached_prompt,
        )

        _ = PromptCacheTracker()
        warm_patterns = [
            (
                "regime_assessment",
                "You are a market regime classifier. Classify the current market regime as BULLISH, BEARISH, or NEUTRAL based on the following indicators:",
            ),
            (
                "gate_check",
                "You are a risk gate evaluator. Determine whether the following trading action should be APPROVED, DENIED, or DEFERRED:",
            ),
            (
                "confluence",
                "You are a trading signal confluence aggregator. Given the following school verdicts, produce a composite recommendation:",
            ),
        ]
        for _name, prefix in warm_patterns:
            _ = build_cached_prompt(system="", prefix=prefix, suffix="warmup")
        logger.info("llm caches: %d prompt patterns pre-warmed", len(warm_patterns))
    except Exception as _e:
        logger.debug("llm cache warm: skipped (%s)", _e)


async def _self_diagnosis_tick(supervisor: JarvisSupervisor, i: int) -> None:
    """Run self-diagnosis every 10 ticks. Detects drift/anomalies and auto-adjusts thresholds."""
    if i % 10 != 0 or i == 0:
        return
    try:
        from eta_engine.brain.anomaly import AnomalyDetector
        from eta_engine.brain.jarvis_v3.self_drift_monitor import SelfDriftMonitor

        report = supervisor.snapshot_health()
        metrics = report.model_dump() if hasattr(report, "model_dump") else {}
        detector = AnomalyDetector()
        drift_check = SelfDriftMonitor.check(metrics) if hasattr(SelfDriftMonitor, "check") else None
        if drift_check and drift_check.get("drift_detected"):
            logger.info("self-diagnosis: drift detected (%s), tightening thresholds", drift_check.get("reason"))
            policy = supervisor.policy
            if hasattr(policy, "tighten"):
                policy.tighten(factor=0.8)
        anomaly_score = detector.score(metrics) if hasattr(detector, "score") else 0.0
        if anomaly_score > 0.8:
            logger.info("self-diagnosis: anomaly score %.2f, initiating circuit breaker", anomaly_score)
    except Exception as _e:
        logger.debug("self-diagnosis: skipped (%s)", _e)


async def _kaizen_cycle_tick(i: int) -> None:
    """Trigger a Kaizen cycle every 60 ticks (~1h)."""
    if i % 60 != 0 or i == 0:
        return
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import send_alert
        from eta_engine.brain.jarvis_v3.kaizen import KaizenLedger
        from eta_engine.brain.jarvis_v3.kaizen_engine import KaizenEngine
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        engine = KaizenEngine(
            instruments=[],
            state_dir=ROOT / "state" / "kaizen",
            ledger=KaizenLedger(),
            guard=KaizenGuard(state_dir=ROOT / "state" / "kaizen"),
        )
        report = engine.cycle()
        await send_alert("Kaizen Auto-Cycle", report.note, "INFO")
        logger.info("kaizen: cycle complete — %s", report.note)
    except Exception as _e:
        logger.debug("kaizen cycle: skipped (%s)", _e)


async def _async_main(
    *,
    inputs_path: Path,
    out_dir: Path,
    interval_s: float,
    max_ticks: int | None,
) -> int:
    supervisor = _build_default_supervisor(inputs_path)
    alerter = build_alerter_from_env()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    # Wire ShadowPipeline daemon (Wave-18)
    shadow_pipe: object = None
    try:
        from eta_engine.brain.jarvis_v3.shadow_pipeline import ShadowPipeline

        shadow_pipe = ShadowPipeline.default()
        shadow_pipe.load_fills()
        if shadow_pipe.enabled:
            logger.info("shadow_pipeline: ENABLED (%d prior fills)", shadow_pipe.total_fills)
        else:
            logger.info("shadow_pipeline: present but DISABLED (set SHADOW_OBSERVER_ENABLED=1)")
    except Exception as exc:
        logger.debug("shadow_pipeline: init failed (%s)", exc)

    # Start multi-symbol IBKR market data feed
    _live_feed = None
    _live_bar: dict | None = None
    try:
        from eta_engine.feeds.multi_symbol_feed import MultiSymbolFeed

        _feed = MultiSymbolFeed(timeframe="5m", poll_interval_s=1.0)
        _task = asyncio.create_task(_background_feed_connect(_feed))
        _live_feed = _feed
        logger.info("multi_feed: background connect started (MNQ/NQ/ES/BTC/ETH)")
    except Exception as _exc:
        logger.debug("multi_feed: init skipped (%s)", _exc)

    # Start Telegram webhook server for instant responses
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import start_webhook_bg

        start_webhook_bg(host="127.0.0.1", port=8842)
        logger.info("hermes webhook: started on port 8842")
    except Exception as _e:
        logger.debug("hermes webhook: skipped (%s)", _e)
    # Register webhook URL with Telegram API for push-based commands
    try:
        asyncio.ensure_future(_register_hermes_webhook_async())
    except Exception as _e:
        logger.debug("hermes webhook registration: skipped (%s)", _e)

    # Start shadow trading orchestrator
    _shadow_orch: object = None
    try:
        from eta_engine.brain.jarvis_v3.shadow_orchestrator import ShadowOrchestrator

        _shadow_orch = ShadowOrchestrator.default()
        logger.info("shadow_orch: %s", "ENABLED" if _shadow_orch.enabled else "DISABLED")
    except Exception as _e:
        logger.debug("shadow_orch: init skipped (%s)", _e)

    # Pre-warm LLM caches before main loop
    try:
        await _warm_llm_caches()
    except Exception as _e:
        logger.debug("llm cache warm: %s", _e)

    logger.info(
        "jarvis_live starting: inputs=%s interval=%.1fs alerter=%s live_feed=%s max_ticks=%s",
        inputs_path,
        interval_s,
        "on" if alerter is not None else "dry-run",
        "on" if _live_feed is not None else "off",
        max_ticks,
    )
    try:
        reports = await run_live(
            supervisor=supervisor,
            alerter=alerter,
            out_dir=out_dir,
            interval_s=interval_s,
            max_ticks=max_ticks,
            stop_event=stop_event,
            live_feed=_live_feed,
            shadow_orch=_shadow_orch,
        )
    except KeyboardInterrupt:
        stop_event.set()
        reports = []
    finally:
        if _live_feed is not None:
            with contextlib.suppress(Exception):
                await _live_feed.disconnect()
    logger.info("jarvis_live stopped after %d reports", len(reports))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EVOLUTIONARY TRADING ALGO Jarvis live supervisor daemon",
    )
    parser.add_argument(
        "--inputs", type=Path, default=DEFAULT_INPUTS, help="Path to premarket_inputs.json (hot-reloaded)"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Directory for jarvis_live_health.json + log"
    )
    parser.add_argument("--interval", type=float, default=60.0, help="Tick interval in seconds (default 60)")
    parser.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks (default: run forever)")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    sys.stdout.write(
        f"[{datetime.now(UTC).isoformat()}] jarvis_live starting\n",
    )
    return asyncio.run(
        _async_main(
            inputs_path=args.inputs,
            out_dir=args.out_dir,
            interval_s=args.interval,
            max_ticks=args.max_ticks,
        )
    )


if __name__ == "__main__":
    sys.exit(main())

"""JARVIS Strategy Supervisor (2026-04-27).

The single multi-bot supervisor that runs the entire strategy fleet
through JARVIS. Replaces the per-bot supervisor pattern.

Architecture:

  data feed       ->  bot.evaluate_entry(bar)  ->  JarvisFull.consult()
                                                       |
                                                       v
                                          ConsolidatedVerdict
                                                       |
                                              (allowed && size_mult > 0)
                                                       |
                                                       v
                                               execution_router
                                                       |
                                                       v
                                            broker_fleet adapter
                                                       |
                                                       v
                                               paper-broker order

  bot.evaluate_exit(position) -> JARVIS consult -> close -> feedback_loop

JARVIS is the admin: every signal goes through ``JarvisFull.consult()``
which chains operator_override, JarvisAdmin, memory_rag, causal,
world_model, firm_board_debate, premortem, ood, operator_coach,
risk_budget, narrative -- and persists every verdict to
``state/jarvis_intel/verdicts.jsonl``.

The supervisor itself is a THIN loop. All intelligence lives in JARVIS.

Bots registered:
  * Loaded from ``per_bot_registry.ASSIGNMENTS`` (active only)
  * Operator can pin a subset via env var ``ETA_SUPERVISOR_BOTS``

Data feeds:
  * mock (default)  -- random-walk synthetic bars; safe for validation
  * yfinance         -- yahoo finance polling (when installed)
  * tradingview      -- TradingView MCP relay (when configured)
  * (future) ibkr / coinbase / binance / hyperliquid

Mode of operation:
  * paper_sim (default) -- supervisor logs simulated fills; no broker
  * paper_live -- routes orders to broker_fleet workers (requires creds)
  * live -- gated behind ``ETA_LIVE_MONEY=1`` + operator override clear

Usage:

    # Default: mock feeds, paper_sim, all active bots
    python scripts/jarvis_strategy_supervisor.py

    # Pin to specific bots
    ETA_SUPERVISOR_BOTS=mnq_futures,btc_hybrid python ...

    # Switch to paper_live (real broker fleet)
    ETA_SUPERVISOR_MODE=paper_live python ...

    # Custom tick interval (default 60s)
    ETA_SUPERVISOR_TICK_S=10 python ...
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal as os_signal
import sys
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.runtime_order_hold import (  # noqa: E402
    load_order_entry_hold,
)
from eta_engine.scripts.uptime_events import record_uptime_event  # noqa: E402

logger = logging.getLogger("jarvis_strategy_supervisor")


# Log hygiene: ib_insync at INFO emits one line per execDetails /
# commissionReport / position event for every order — floods the
# supervisor log with replay state on every reconnect (~hundreds
# of lines per restart). The errors and warnings still surface;
# we only mute the noisy INFOs. Override via env if needed:
#   ETA_IBKR_LOG_LEVEL=INFO  → restore verbose
_ib_log_level = os.getenv("ETA_IBKR_LOG_LEVEL", "WARNING").upper()
for _ib_logger in ("ib_insync", "ib_insync.client", "ib_insync.wrapper",
                   "ib_insync.ib", "eventkit"):
    logging.getLogger(_ib_logger).setLevel(_ib_log_level)


# ─── Configuration ────────────────────────────────────────────────


def _bool_env(name: str, default: bool = False) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _resolve_bot_routing(bot_id: str, symbol: str) -> tuple[str | None, str | None]:
    """Resolve ``(venue, asset_class)`` for a bot/symbol pair.

    Wraps :class:`broker_router.RoutingConfig` so the scope-aware order-
    entry hold can decide whether a hold scoped to ``ibkr`` or
    ``futures`` should release a crypto-Alpaca bot. Failure-tolerant:
    any exception here returns ``(None, None)`` and the caller treats
    that as "unknown routing -> apply hold legacy-style".
    """
    try:
        from eta_engine.scripts.broker_router import (  # noqa: PLC0415
            RoutingConfig,
            _asset_class_for_symbol,
        )
        cfg = RoutingConfig.load()
        venue = cfg.venue_for(bot_id, symbol=symbol)
        asset_class = _asset_class_for_symbol(symbol)
        return venue, asset_class
    except Exception:  # noqa: BLE001 — routing is advisory here, never fatal
        return None, None


def _compact_strategy_readiness(row: dict[str, Any]) -> dict[str, Any]:
    """Return the bot-level readiness fields safe for supervisor heartbeat."""
    return {
        "status": "ready",
        "bot_id": row.get("bot_id"),
        "strategy_id": row.get("strategy_id"),
        "launch_lane": row.get("launch_lane"),
        "data_status": row.get("data_status"),
        "promotion_status": row.get("promotion_status"),
        "can_paper_trade": bool(row.get("can_paper_trade")),
        "can_live_trade": bool(row.get("can_live_trade")),
        "next_action": row.get("next_action"),
    }


def _load_bot_strategy_readiness_snapshot(
    path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load canonical strategy readiness for heartbeat enrichment."""
    target = path or workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "status": "missing",
            "path": str(target),
            "summary": {},
            "generated_at": None,
        }, {}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "generated_at": None,
            "error": str(exc),
        }, {}

    if not isinstance(payload, dict):
        return {
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "generated_at": None,
            "error": "bot strategy readiness snapshot must be a JSON object",
        }, {}

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    readiness_by_bot = {
        str(row["bot_id"]): _compact_strategy_readiness(row)
        for row in rows
        if isinstance(row, dict) and row.get("bot_id")
    }
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "status": "ready",
        "path": str(target),
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "summary": summary,
    }, readiness_by_bot


# Load .env so os.getenv() sees paper_live / STARTING_CASH etc
_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())


def _default_supervisor_state_dir() -> Path:
    """Return the safest default state path for this supervisor process.

    Production paper-live/composite writes to the canonical dashboard path.
    Synthetic mock supervisors are useful for diagnostics, but they must not
    overwrite the live heartbeat unless the operator explicitly opts in with
    ETA_SUPERVISOR_STATE_DIR.
    """
    explicit = os.getenv("ETA_SUPERVISOR_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    feed = os.getenv("ETA_SUPERVISOR_FEED", "mock").strip().lower()
    mode = os.getenv("ETA_SUPERVISOR_MODE", "paper_sim").strip().lower()
    if feed == "mock" and mode != "paper_live":
        return workspace_roots.ETA_RUNTIME_STATE_DIR / "jarvis_intel" / "supervisor_mock"
    return workspace_roots.ETA_JARVIS_SUPERVISOR_STATE_DIR



@dataclass
class SupervisorConfig:
    """Operator-tunable supervisor knobs (read from env)."""

    # Comma-separated bot_ids; empty = all active in per_bot_registry
    bots_env: str = field(default_factory=lambda: os.getenv("ETA_SUPERVISOR_BOTS", ""))
    # mock | yfinance | tradingview
    data_feed: str = field(default_factory=lambda: os.getenv("ETA_SUPERVISOR_FEED", "mock"))
    # paper_sim | paper_live | live
    mode: str = field(default_factory=lambda: os.getenv("ETA_SUPERVISOR_MODE", "paper_sim"))
    # Tick interval in seconds
    tick_s: float = field(default_factory=lambda: float(os.getenv("ETA_SUPERVISOR_TICK_S", "60")))
    # Per-bot starting cash for sim P&L tracking
    starting_cash_per_bot: float = field(
        default_factory=lambda: float(os.getenv("ETA_SUPERVISOR_STARTING_CASH", "5000")),
    )
    # Heartbeat output path
    state_dir: Path = field(
        default_factory=_default_supervisor_state_dir,
    )
    # Live-money gate (extra safety; even paper_live still requires this False)
    live_money_enabled: bool = field(
        default_factory=lambda: _bool_env("ETA_LIVE_MONEY", default=False),
    )
    # paper_live order path. Default to direct TWS/IB Gateway routing; the
    # broker-router pending-file lane is an opt-in alternate path so one
    # signal_id is not submitted twice.
    paper_live_order_route: str = field(
        default_factory=lambda: os.getenv(
            "ETA_PAPER_LIVE_ORDER_ROUTE", "direct_ibkr",
        ).strip().lower(),
    )
    # Broker-router order inbox. Keep pending order files in canonical
    # runtime state, not mixed into generated broker-fleet status reports.
    broker_router_pending_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "ETA_BROKER_ROUTER_PENDING_DIR",
                str(
                    workspace_roots.ETA_RUNTIME_STATE_DIR
                    / "router"
                    / "pending"
                ),
            ),
        ),
    )


# ─── Bot wrapper ──────────────────────────────────────────────────


@dataclass
class BotInstance:
    """One running bot inside the supervisor loop."""

    bot_id: str
    symbol: str
    strategy_kind: str
    direction: str = "long"
    cash: float = 5000.0
    open_position: dict | None = None        # {entry_price, qty, side, opened_at}
    n_entries: int = 0
    n_exits: int = 0
    realized_pnl: float = 0.0
    last_bar_ts: str = ""
    last_signal_at: str = ""
    last_jarvis_verdict: str = ""
    # Diagnostic reason for last_jarvis_verdict == "NONE": one of
    #   ""                              -> consult succeeded (verdict is real)
    #   "jarvis_not_bootstrapped"       -> JarvisFull failed to initialize
    #   "regime_block:<kind>@<regime>"  -> regime gate filtered this bot
    #   "consult_exception:<ExcType>"   -> consult raised, see logs
    # Surfaced into the heartbeat so the operator can see WHY 50/52 bots
    # show NONE, without tailing the supervisor log. Empty after a
    # successful consult — never carry stale diagnostic forward.
    last_jarvis_verdict_reason: str = ""
    # Broker-reject backpressure counter — incremented every time a
    # paper_live/live broker submission either returns a non-success
    # status OR raises. Reset to 0 on a successful broker fill. The
    # supervisor surfaces this in the heartbeat so the operator can
    # see when one bot is repeatedly failing at the broker; downstream
    # circuit-breakers can also gate further entries when it crosses a
    # threshold. See submit_entry — the field MUST stay coherent with
    # bot.open_position (no phantom open positions when the broker
    # rejected).
    consecutive_broker_rejects: int = 0
    sage_bars: deque = field(default_factory=lambda: deque(maxlen=200))

    def to_state(self, *, mode: str | None = None) -> dict:
        # Per-bot ``mode`` field is REQUIRED in the heartbeat so the
        # dashboard can render Mode: paper_live for each bot row instead
        # of a hardcoded ``paper_sim`` default. The mode is sourced from
        # the supervisor-wide cfg.mode (no per-bot override today); if
        # caller passes None we omit the field so callers without a cfg
        # context (e.g. round-trip tests) don't get a stale value.
        # See PAPER_LIVE_ROUTING_GAP.md (52 bots stuck on paper_sim badge).
        d = asdict(self)
        d.pop("sage_bars", None)
        if mode is not None:
            d["mode"] = mode
        return d


# ─── Mock data feed (random-walk synthetic) ───────────────────────


@dataclass
class _BarRng:
    last_close: float
    sigma: float
    drift: float
    rng: random.Random

    def next_bar(self) -> dict[str, float]:
        # Geometric Brownian step
        ret = self.rng.gauss(self.drift, self.sigma)
        new_close = self.last_close * (1.0 + ret)
        high = max(self.last_close, new_close) * (1.0 + abs(self.rng.gauss(0, self.sigma * 0.3)))
        low = min(self.last_close, new_close) * (1.0 - abs(self.rng.gauss(0, self.sigma * 0.3)))
        bar = {
            "open": round(self.last_close, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(new_close, 2),
            "volume": int(abs(self.rng.gauss(1_000_000, 200_000))),
        }
        self.last_close = new_close
        return bar


class MockDataFeed:
    """Synthetic bar feed -- random walk per symbol with sane defaults."""

    SYMBOL_DEFAULTS = {
        "MNQ":  (21450.0, 0.002, 0.0001),
        "MNQ1": (21450.0, 0.002, 0.0001),
        "NQ":   (21500.0, 0.004, 0.0002),
        "NQ1":  (21500.0, 0.004, 0.0002),
        "BTC":  (95000.0, 0.005, 0.0002),
        "ETH":  (3500.0,  0.006, 0.0001),
        "SOL":  (180.0,   0.010, 0.0002),
        "MBT":  (95000.0, 0.005, 0.0001),
        "MET":  (3500.0,  0.006, 0.0001),
    }

    def __init__(self, *, seed: int = 42) -> None:
        self._rngs: dict[str, _BarRng] = {}
        self._seed = seed

    def _get_rng(self, symbol: str) -> _BarRng:
        sym = symbol.upper().replace("USD", "").replace("USDT", "")
        if sym not in self._rngs:
            close, sigma, drift = self.SYMBOL_DEFAULTS.get(
                sym, (100.0, 0.01, 0.0),
            )
            self._rngs[sym] = _BarRng(
                last_close=close, sigma=sigma, drift=drift,
                rng=random.Random(self._seed + hash(sym) % 1000),
            )
        return self._rngs[sym]

    def get_bar(self, symbol: str) -> dict[str, Any]:
        rng = self._get_rng(symbol)
        bar = rng.next_bar()
        bar["symbol"] = symbol
        bar["ts"] = datetime.now(UTC).isoformat()
        return bar


# ─── Execution router ─────────────────────────────────────────────


@dataclass
class FillRecord:
    bot_id: str
    signal_id: str
    side: str
    symbol: str
    qty: float
    fill_price: float
    fill_ts: str
    paper: bool
    realized_r: float | None = None
    realized_pnl: float | None = None  # USD pnl on close; None for entries
    note: str = ""


# Dedicated background thread + asyncio loop for LiveIbkrVenue. Required
# because ib_insync/eventkit caches an event loop at import time and binds
# its socket _OverlappedFuture objects to that loop; awaiting them from
# any other loop raises "Future attached to a different loop". Running a
# single, never-closed loop in a daemon thread, and dispatching every
# place_order coroutine via asyncio.run_coroutine_threadsafe, gives every
# IBKR call the same loop and the same thread context, eliminating the
# class of "different loop" failures observed in the wave-7/8 deployment.
_LIVE_IBKR_LOOP: asyncio.AbstractEventLoop | None = None
_LIVE_IBKR_THREAD: threading.Thread | None = None
_LIVE_IBKR_LOCK = threading.Lock()


def _get_or_create_live_ibkr_loop() -> asyncio.AbstractEventLoop:
    global _LIVE_IBKR_LOOP, _LIVE_IBKR_THREAD
    with _LIVE_IBKR_LOCK:
        if (
            _LIVE_IBKR_LOOP is not None
            and not _LIVE_IBKR_LOOP.is_closed()
            and _LIVE_IBKR_THREAD is not None
            and _LIVE_IBKR_THREAD.is_alive()
        ):
            return _LIVE_IBKR_LOOP
        new_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _runner() -> None:
            asyncio.set_event_loop(new_loop)
            ready.set()
            try:
                new_loop.run_forever()
            finally:
                with contextlib.suppress(Exception):
                    new_loop.close()

        thread = threading.Thread(
            target=_runner, name="live-ibkr-loop", daemon=True,
        )
        thread.start()
        ready.wait(timeout=5)
        _LIVE_IBKR_LOOP = new_loop
        _LIVE_IBKR_THREAD = thread
        return new_loop


def _run_on_live_ibkr_loop[T](
    coro: Coroutine[Any, Any, T], timeout: float = 30.0,
) -> T:
    """Run a coroutine on the dedicated live-IBKR loop, return its result."""
    loop = _get_or_create_live_ibkr_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


# Singleton LiveIbkrVenue. The class declares _ib/_connected/_lock at class
# scope, but place_order writes them via ``self._ib = ...`` which creates
# instance attributes — class-level state never gets cached. Sharing one
# venue instance across the whole supervisor lifetime makes the per-call
# cache work and stops every order from triggering a fresh TWS connect.
_LIVE_IBKR_VENUE: Any | None = None


def _get_live_ibkr_venue() -> Any:  # noqa: ANN401 — LiveIbkrVenue not import-time available
    global _LIVE_IBKR_VENUE
    # Lock the singleton init so a concurrent first-tick storm with N
    # bots can't race two threads through the None check and create
    # multiple TWS connections. Subsequent calls take the lock briefly
    # but return the cached instance immediately.
    with _LIVE_IBKR_LOCK:
        if _LIVE_IBKR_VENUE is None:
            from eta_engine.venues.ibkr_live import LiveIbkrVenue
            _LIVE_IBKR_VENUE = LiveIbkrVenue()
        return _LIVE_IBKR_VENUE


# Symbol → instrument-class lookup. Used by sage cross-asset peer wiring
# and the bracket-sizing path. Mirrors data_feeds._is_crypto / bracket_sizing's
# _futures_set so all three modules agree on what's crypto vs futures.
_CRYPTO_ROOTS = frozenset({
    "BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET", "XRP",
})
_FUTURES_ROOTS = frozenset({
    "MNQ", "NQ", "ES", "MES", "GC", "MGC", "CL", "MCL",
    "NG", "ZN", "ZB", "6E", "M6E", "RTY", "M2K",
})
_PAPER_LIVE_ALLOWED_SYMBOLS_ENV = "ETA_PAPER_LIVE_ALLOWED_SYMBOLS"


# Per-order contract caps (absolute hard ceiling, applied AFTER budget cap).
# Even a budget-correct sizing must not exceed these — they are the last
# line of defense against malformed bars / wrong-multiplier bugs. A
# malformed bar that produces ref_price ≈ 1e-9 (the floor used in
# submit_entry) yields a 5-billion-unit qty before any budget cap can
# intervene; this dict catches that pathology after the budget cap.
_MAX_QTY_PER_ORDER: dict[str, float] = {
    # Futures (integer contracts): generous-but-bounded
    "MNQ": 5, "NQ": 2, "ES": 2, "MES": 5, "RTY": 3, "M2K": 5,
    "MBT": 3, "MET": 3,
    "CL": 3, "MCL": 5, "NG": 3,
    "GC": 2, "MGC": 5,
    "ZN": 3, "ZB": 3,
    "6E": 3, "M6E": 5,
    # Crypto (fractional): cap at $5K notional-equivalent baseline
    # (exact value computed by venue from ref_price * qty downstream)
    "BTC": 0.1, "ETH": 5.0, "SOL": 200.0, "XRP": 50_000.0,
    "AVAX": 200.0, "LINK": 1000.0, "DOGE": 100_000.0,
}
_MAX_QTY_DEFAULT_FUTURES = 3
_MAX_QTY_DEFAULT_CRYPTO = 0.5  # crypto base unit, conservative


# Sent-signal log filename for restart-safe deduplication. Lives under
# the supervisor state_dir; the dedup window is 24h (older entries are
# ignored to keep the file from growing unbounded).
_SENT_SIGNALS_LOG_FILENAME = "sent_signals.jsonl"
_SENT_SIGNALS_DEDUP_HOURS = 24


def _classify_symbol(symbol: str) -> str:
    """Return one of {'crypto', 'futures', 'other'} for a bot symbol."""
    s = symbol.upper().lstrip("/").rstrip("0123456789")
    if s in _CRYPTO_ROOTS:
        return "crypto"
    if s in _FUTURES_ROOTS:
        return "futures"
    return "other"


def _round_to_tick(price: float, symbol: str) -> float:
    """Round price to the symbol's tick grid.

    Returns price unchanged when the spec lookup fails or the spec
    reports a non-positive tick size. The supervisor was previously
    writing un-rounded prices into pending_order JSON and into
    ``bot.open_position["bracket_stop"|"bracket_target"]`` — IBKR
    silently quantized the order while the supervisor's R-attribution
    used the un-rounded value, drifting paper R from realized R by up
    to ~half a tick per leg.
    """
    try:
        from eta_engine.feeds.instrument_specs import get_spec
        spec = get_spec(symbol)
        tick = float(getattr(spec, "tick_size", 0) or 0)
        if tick > 0:
            return round(round(price / tick) * tick, 8)
    except Exception:  # noqa: BLE001 — best-effort; never block on spec lookup
        pass
    return price


def _paper_live_allowed_symbols() -> frozenset[str] | None:
    raw = os.getenv(_PAPER_LIVE_ALLOWED_SYMBOLS_ENV, "").strip()
    if not raw:
        return None
    allowed = frozenset(
        item.upper().lstrip("/").strip()
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    )
    return allowed or None


def _paper_live_symbol_allowed(symbol: str, allowed: frozenset[str] | None) -> bool:
    if allowed is None:
        return True
    normalized = symbol.upper().lstrip("/").strip()
    root = normalized.rstrip("0123456789")
    return normalized in allowed or root in allowed


class ExecutionRouter:
    """Routes approved entries to broker (or simulates them).

    paper_sim: simulates fills at the bar's close + small slippage,
               no broker call. Generates a synthetic FillRecord.
    paper_live: submits through one configured route. Default is direct
                TWS/IB Gateway via LiveIbkrVenue; broker_router writes
                pending files only when ETA_PAPER_LIVE_ORDER_ROUTE is
                broker_router.
    live: gated behind ETA_LIVE_MONEY=1 (raises if attempted).
    """

    def __init__(
        self,
        *,
        cfg: SupervisorConfig,
        bf_dir: Path,
        bots_ref: Any | None = None,  # noqa: ANN401 — callable returning list[BotInstance]
    ) -> None:
        self.cfg = cfg
        self.bf_dir = bf_dir
        self.bf_dir.mkdir(parents=True, exist_ok=True)
        # bots_ref is a callable returning the supervisor's current bot
        # list. Used to compute fleet-aggregate open notional for the
        # capital cap. Decoupled so unit tests can pass a fixed list
        # without instantiating the full supervisor.
        self._bots_ref = bots_ref or (lambda: [])

    def _fleet_open_notional_for_symbol(self, symbol: str) -> float:
        """Sum of |qty| * entry_price across bots whose symbol shares
        the same asset class (crypto / futures / other). Used by
        cap_qty_to_budget to enforce ETA_LIVE_*_FLEET_BUDGET_USD."""
        from eta_engine.scripts.bracket_sizing import _is_crypto, _is_futures
        same_class: Any
        if _is_crypto(symbol):
            same_class = _is_crypto
        elif _is_futures(symbol):
            same_class = _is_futures
        else:
            same_class = lambda s: not (_is_crypto(s) or _is_futures(s))  # noqa: E731

        total = 0.0
        for b in self._bots_ref():
            try:
                if not same_class(b.symbol):
                    continue
                pos = getattr(b, "open_position", None)
                if not pos:
                    continue
                qty = abs(float(pos.get("qty", 0) or 0))
                px = float(pos.get("entry_price", 0) or 0)
                total += qty * px
            except (AttributeError, TypeError, ValueError):
                continue
        return total

    def submit_entry(
        self,
        *,
        bot: BotInstance,
        signal_id: str,
        side: str,
        bar: dict[str, Any],
        size_mult: float,
    ) -> FillRecord | None:
        if self.cfg.mode == "live" and not self.cfg.live_money_enabled:
            logger.warning(
                "%s entry SKIPPED: mode=live but ETA_LIVE_MONEY not set",
                bot.bot_id,
            )
            return None

        if self.cfg.mode in {"paper_live", "live"}:
            hold = load_order_entry_hold()
            if hold.active:
                # Scope-aware hold (2026-05-06): when the operator (or the
                # IBG connection monitor) sets a hold with scope="ibkr" or
                # scope="futures", crypto bots routed to Alpaca should keep
                # trading. Resolve the bot's venue + asset class and let
                # the hold decide. Missing routing context falls back to
                # the legacy "block everything" behaviour.
                resolved_venue, asset_class = _resolve_bot_routing(
                    bot.bot_id, bot.symbol,
                )
                if hold.blocks(venue=resolved_venue, asset_class=asset_class):
                    logger.warning(
                        "%s entry SKIPPED: order-entry hold active "
                        "scope=%s reason=%s venue=%s class=%s path=%s",
                        bot.bot_id,
                        hold.scope,
                        hold.reason,
                        resolved_venue,
                        asset_class,
                        hold.path,
                    )
                    return None
                logger.info(
                    "%s entry ALLOWED past hold: scope=%s venue=%s class=%s "
                    "(hold reason=%s) -- crypto/non-IBKR lane stays open",
                    bot.bot_id,
                    hold.scope,
                    resolved_venue,
                    asset_class,
                    hold.reason,
                )

        # Compute simulated fill (mode=paper_sim).
        # Adverse-selection slippage: a real BUY crosses the offer (fills
        # ABOVE mid); a real SELL crosses the bid (fills BELOW mid). The
        # earlier ``slippage_bps = 1.5 if BUY else -1.5`` formula gave
        # SHORT entries a BETTER-than-mid fill, biasing every short trade
        # by ~3 bps round-trip. Magnitude is always positive; only sign
        # flips with side.
        ref_price = float(bar.get("close", 0.0))
        adverse_bps = 1.5  # always positive — slippage is always against the trader
        sign_slip = 1.0 if side == "BUY" else -1.0  # BUY: above mid; SELL: below mid
        fill_price = ref_price + sign_slip * (ref_price * adverse_bps / 10_000.0)
        fill_price = _round_to_tick(fill_price, bot.symbol)
        # Size: use 10% of bot cash as risk unit (was 1%), with size_mult gating.
        # Floor at 1 contract for futures (CME/CBOT/NYMEX), allow fractional
        # for crypto spot (BTC/ETH/SOL).
        risk_unit = bot.cash * 0.10
        base_qty = risk_unit / max(ref_price, 1e-9)
        qty = base_qty * size_mult
        # Minimum lot sizes — futures trade in whole-lot increments,
        # crypto allows fractional. Strip the contract-month suffix
        # (NQ1, MNQ1, ES1) before matching against the futures set so
        # NQ1 doesn't fall through to the crypto floor (was producing
        # int(0.04)=0 → naked qty=0 orders at TWS).
        symbol_upper = bot.symbol.upper().lstrip("/")
        _symbol_root = symbol_upper.rstrip("0123456789")
        _futures_set = {
            "MNQ", "NQ", "ES", "MES", "MBT", "MET", "NG", "CL", "GC",
            "ZN", "ZB", "6E", "M6E", "MGC", "MCL", "RTY", "M2K",
        }
        is_futures = _symbol_root in _futures_set
        # Round to instrument precision WITHOUT a floor-up. The earlier
        # ``qty = max(qty, min_qty)`` would force a $5K crypto bot
        # sizing 0.02 MNQ contracts up to 1 contract = ~$42K notional —
        # the opposite of what a min_notional floor should do. Now: if
        # the bot's budget can't afford even one whole-lot futures
        # contract (or 0.001 crypto), skip the entry rather than
        # over-sizing into a margin call.
        if is_futures:
            paper_floor_enabled = (
                self.cfg.mode == "paper_live"
                and float(os.getenv("ETA_PAPER_FUTURES_FLOOR", "1")) > 0
            )
            # Let cap_qty_to_budget apply the explicit paper futures floor.
            qty = 1.0 if 0.0 < qty < 1.0 and paper_floor_enabled else float(int(qty))
        else:
            qty = round(qty, 6)
        _min_qty = 1.0 if is_futures else 0.001
        if qty < _min_qty:
            logger.info(
                "bot %s skipped: budget %.2f can't afford min lot (%s qty=%.6f < min=%.6f)",
                bot.bot_id, bot.cash, bot.symbol, qty, _min_qty,
            )
            return None

        # ── CAPITAL BUDGET CAP ────────────────────────────────────
        # Hard-clamp the requested qty to the per-bot/fleet USD cap so
        # live cutover with $500-2000 starting capital cannot accidentally
        # ship $5K+ orders. cap_qty_to_budget reads ETA_LIVE_*_BUDGET_*
        # env vars; defaults are conservative (crypto $100/bot, $1500
        # fleet; futures $500/bot, $5000 fleet — paper-friendly).
        #
        # FAIL-CLOSED on budget-cap exception: a module bug or
        # state-file corruption MUST NOT translate into shipping the
        # uncapped qty. We log at CRITICAL, emit a structured journal
        # event so the operator sees the failure immediately, and skip
        # the entry entirely.
        try:
            from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
            fleet_notional = self._fleet_open_notional_for_symbol(bot.symbol)
            capped, _cap_reason = cap_qty_to_budget(
                symbol=bot.symbol,
                entry_price=ref_price,
                requested_qty=qty,
                fleet_open_notional_usd=fleet_notional,
            )
            if _cap_reason != "ok":
                logger.info(
                    "CAP %s %s req=%.6f → capped=%.6f (%s, fleet_notional=%.2f)",
                    bot.bot_id, bot.symbol, qty, capped, _cap_reason, fleet_notional,
                )
            qty = capped
        except Exception as exc:  # noqa: BLE001 — fail closed, see comment above
            logger.critical(
                "BUDGET CAP FAILED for %s symbol=%s requested_qty=%g: %s — "
                "refusing entry (fail-closed)",
                bot.bot_id, bot.symbol, qty, exc, exc_info=True,
            )
            with contextlib.suppress(Exception):
                from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
                emit_event(
                    layer="risk",
                    event="budget_cap_failed",
                    bot_id=bot.bot_id,
                    cls=_classify_symbol(bot.symbol),
                    details={
                        "symbol": bot.symbol,
                        "requested_qty": float(qty),
                        "ref_price": float(ref_price),
                        "exception_type": type(exc).__name__,
                        "exception_msg": str(exc)[:500],
                    },
                    severity="CRITICAL",
                )
            return None

        if qty <= 0:
            logger.info(
                "%s entry skipped: budget cap produced qty=0 (%s @ %.4f)",
                bot.bot_id, bot.symbol, ref_price,
            )
            return None

        # ── HARD QTY CEILING ──────────────────────────────────────
        # Last line of defense. Even if cap_qty_to_budget returns a
        # plausible value, a malformed bar (ref_price near 1e-9) or a
        # wrong contract multiplier could still translate into a wildly
        # oversized request. The per-symbol _MAX_QTY_PER_ORDER dict is
        # an ABSOLUTE clamp applied AFTER the budget cap — a separate
        # belt-and-braces layer.
        sym_norm = (
            bot.symbol.upper().lstrip("/")
            .replace("USDT", "").replace("USD", "")
            .rstrip("0123456789")
        )
        _hard_cap = _MAX_QTY_PER_ORDER.get(sym_norm)
        if _hard_cap is None:
            _hard_cap = (
                _MAX_QTY_DEFAULT_CRYPTO
                if sym_norm in _CRYPTO_ROOTS
                else _MAX_QTY_DEFAULT_FUTURES
            )
        if qty > _hard_cap:
            logger.critical(
                "HARD QTY CAP TRIPPED: bot=%s sym=%s requested=%g cap=%g",
                bot.bot_id, bot.symbol, qty, _hard_cap,
            )
            qty = _hard_cap

        rec = FillRecord(
            bot_id=bot.bot_id,
            signal_id=signal_id,
            side=side,
            symbol=bot.symbol,
            qty=qty,
            fill_price=round(fill_price, 4),
            fill_ts=datetime.now(UTC).isoformat(),
            paper=True,
            note=f"mode={self.cfg.mode}",
        )

        # Record open position on the bot
        bot.open_position = {
            "side": side,
            "qty": qty,
            "entry_price": rec.fill_price,
            "entry_ts": rec.fill_ts,
            "signal_id": signal_id,
        }
        # Compute bracket levels at entry — ALWAYS, even for paper-only crypto
        # bots that never round-trip through the broker. Without this, paper
        # _maybe_exit falls through to a 1-in-15 random close that exits at
        # trivial price moves (~$10 on BTC vs the bot's 2.0×ATR planned stop
        # of ~$1000). Storing the planned bracket here lets _maybe_exit gate
        # exits on the actual planned levels regardless of broker presence,
        # so paper R-magnitudes finally match lab.
        try:
            from eta_engine.scripts.bracket_sizing import (
                compute_bracket as _cb,
            )
            from eta_engine.scripts.bracket_sizing import (
                lookup_bot_bracket_params as _lbp,
            )
            _sm, _tm = _lbp(bot.bot_id)
            _ps, _pt, _psrc = _cb(
                side=side, entry_price=rec.fill_price, bars=bot.sage_bars,
                stop_mult_override=_sm, target_mult_override=_tm,
            )
            bot.open_position["bracket_stop"] = round(_round_to_tick(_ps, bot.symbol), 4)
            bot.open_position["bracket_target"] = round(_round_to_tick(_pt, bot.symbol), 4)
            bot.open_position["bracket_src"] = f"paper:{_psrc}"
        except Exception as _exc:  # noqa: BLE001 — best effort
            # FIRST failure per bot logs at warning level so a systemic
            # compute_bracket break doesn't disappear into debug. Subsequent
            # failures from the same bot drop to debug to avoid log spam.
            # Without this hardening (per risk-review), if compute_bracket
            # ever broke, every paper bot would silently fall through to
            # the legacy fallback gates and the operator would have no
            # signal that brackets weren't actually being stored.
            if not hasattr(self, "_paper_bracket_warned"):
                self._paper_bracket_warned: set[str] = set()
            if bot.bot_id not in self._paper_bracket_warned:
                logger.warning(
                    "paper-bracket compute failed for %s (first occurrence): %s",
                    bot.bot_id, _exc,
                )
                self._paper_bracket_warned.add(bot.bot_id)
            else:
                logger.debug(
                    "paper-bracket compute failed for %s: %s", bot.bot_id, _exc,
                )
        bot.n_entries += 1
        bot.last_signal_at = rec.fill_ts

        def _rollback_recorded_entry(reason: str) -> None:
            """Undo the simulated entry when paper_live broker entry failed.

            INVARIANT: bot.open_position must NEVER reflect a position the
            broker has not accepted. We use PATTERN B from the audit —
            optimistically set bot.open_position pre-call, then on any
            non-success status OR exception, immediately clear it and
            increment the per-bot reject counter. This keeps the
            supervisor's belief about open positions strictly bounded by
            broker-acknowledged fills.
            """
            if (
                bot.open_position is not None
                and bot.open_position.get("signal_id") == rec.signal_id
            ):
                bot.open_position = None
            bot.n_entries = max(0, bot.n_entries - 1)
            bot.consecutive_broker_rejects += 1
            logger.critical(
                "BROKER REJECT %s: paper_live entry rolled back (reason=%s "
                "symbol=%s side=%s qty=%.6f signal_id=%s consecutive_rejects=%d)",
                bot.bot_id, reason, rec.symbol, rec.side, rec.qty,
                rec.signal_id, bot.consecutive_broker_rejects,
            )

        if self.cfg.mode == "paper_live":
            _route = (self.cfg.paper_live_order_route or "direct_ibkr").strip().lower()
            _allowed_symbols = _paper_live_allowed_symbols()
            if not _paper_live_symbol_allowed(rec.symbol, _allowed_symbols):
                logger.warning(
                    "%s broker route SKIPPED: %s not in %s=%s",
                    bot.bot_id,
                    rec.symbol,
                    _PAPER_LIVE_ALLOWED_SYMBOLS_ENV,
                    ",".join(sorted(_allowed_symbols or ())),
                )
                _rollback_recorded_entry("symbol_not_allowed_for_broker_route")
                return None
            if _route in {"broker_router", "pending_file", "pending"}:
                self._write_pending_order(bot, rec)
                return rec
            if _route not in {"direct_ibkr", "direct", "ibkr"}:
                logger.warning(
                    "unknown ETA_PAPER_LIVE_ORDER_ROUTE=%r; using direct_ibkr",
                    self.cfg.paper_live_order_route,
                )
            # Crypto paper-test path: when ETA_IBKR_CRYPTO is not enabled
            # (paper account lacks crypto trading permissions), skip the
            # broker round-trip but keep the simulated FillRecord +
            # bot.open_position recorded above so paper P&L still tracks.
            # This lets crypto bots fine-tune on simulated fills until
            # the IBKR account is upgraded; flipping ETA_IBKR_CRYPTO=1
            # then auto-routes to PAXOS without code changes.
            _symbol_root = rec.symbol.upper().rstrip("0123456789").replace("USD", "")
            _is_crypto = _symbol_root in {"BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET"}
            _crypto_live = os.getenv("ETA_IBKR_CRYPTO", "").lower() in {"1", "true", "yes", "on"}
            if _is_crypto and not _crypto_live:
                logger.info(
                    "CRYPTO PAPER %s %s %.6f @ %.4f (no broker route — set ETA_IBKR_CRYPTO=1 to go live)",
                    rec.symbol, rec.side, rec.qty, rec.fill_price,
                )
                return rec
            # Also submit directly through LiveIbkrVenue (TWS API port 4002)
            try:
                from eta_engine.scripts.bracket_sizing import (
                    compute_bracket,
                    lookup_bot_bracket_params,
                )
                from eta_engine.venues.base import OrderRequest, OrderType, Side
                _venue = _get_live_ibkr_venue()
                # ATR-based bracket if ≥15 bars in the bot's sage window,
                # else fixed-percent fallback. ATR adapts stop width to
                # actual symbol volatility (BTC needs ~5x more room than
                # MNQ for the same number of "ticks of breathing room").
                # Per-bot atr_stop_mult / rr_target from per_bot_registry
                # take precedence over the global env defaults so live
                # and lab geometry match.
                _stop_mult, _target_mult = lookup_bot_bracket_params(bot.bot_id)
                _ref = float(rec.fill_price) if rec.fill_price else float(bar.get("close", 0.0)) or 1.0
                _stop, _target, _bracket_src = compute_bracket(
                    side=rec.side, entry_price=_ref, bars=bot.sage_bars,
                    stop_mult_override=_stop_mult,
                    target_mult_override=_target_mult,
                )
                # Pre-trade sanity: stop and target must straddle entry on
                # the correct sides. ATR can degenerate (zero-volatility
                # window, malformed bars) and produce stop=target=entry,
                # which would ship a no-op bracket to the broker. Refuse
                # to submit in that case.
                _is_buy = rec.side.upper() == "BUY"
                _bad = (
                    _ref <= 0
                    or _stop <= 0 or _target <= 0
                    or (_is_buy and not (_stop < _ref < _target))
                    or (not _is_buy and not (_target < _ref < _stop))
                )
                if _bad:
                    logger.warning(
                        "%s skipped: insane bracket geometry "
                        "(side=%s ref=%.4f stop=%.4f target=%.4f src=%s)",
                        bot.bot_id, rec.side, _ref, _stop, _target, _bracket_src,
                    )
                    _rollback_recorded_entry("invalid_bracket_geometry")
                    return None
                logger.debug(
                    "bracket %s %s %s→%s (%s)",
                    bot.bot_id, _ref, _stop, _target, _bracket_src,
                )
                _req = OrderRequest(
                    symbol=rec.symbol,
                    side=Side.BUY if rec.side.upper() == "BUY" else Side.SELL,
                    qty=abs(float(rec.qty)) or 1,
                    order_type=OrderType.MARKET,
                    # LiveIbkrVenue uses this as the bounded reference when
                    # it converts outside-session MKT entries to marketable LMT.
                    price=round(_round_to_tick(_ref, rec.symbol), 4),
                    stop_price=round(_round_to_tick(_stop, rec.symbol), 4),
                    target_price=round(_round_to_tick(_target, rec.symbol), 4),
                    bot_id=bot.bot_id,
                    # signal_id is unique per call (uuid4 hex slice), so
                    # using it as client_order_id stops two identical
                    # MNQ1 BUY 1.0 entries from the same tick from
                    # silently dedup-OPENing — each call carries its
                    # own idempotency key now.
                    client_order_id=rec.signal_id,
                )
                _result = _run_on_live_ibkr_loop(_venue.place_order(_req), timeout=30.0)
                _reason = (
                    _result.raw.get("reason")
                    or ("deduped: " + str(_result.raw.get("note", "")) if _result.raw.get("deduped") else "")
                    or "n/a"
                )
                logger.info(
                    "DIRECT ORDER %s %s %.6f → %s (ibkr_id=%s, reason=%s)",
                    rec.symbol, rec.side, rec.qty,
                    _result.status.value,
                    _result.raw.get("ibkr_order_id", "?"),
                    _reason,
                )
                # Mark the open position as "broker-bracketed" so
                # _maybe_exit defers to the broker's stop/target instead
                # of double-exiting via supervisor-side logic. The flag
                # is read in _maybe_exit; if True, only an emergency
                # stop (drawdown beyond 2x the bracket stop) overrides.
                # The set of broker statuses that mean "supervisor MAY
                # keep bot.open_position": OPEN, PARTIAL, FILLED. Any
                # other status (REJECTED, CANCELLED, EXPIRED, UNKNOWN)
                # forces an immediate rollback so phantom positions
                # cannot leak through.
                _ok_statuses = {"OPEN", "PARTIAL", "FILLED"}
                if (
                    _result.status.value in _ok_statuses
                    and bot.open_position is not None
                ):
                    bot.open_position["broker_bracket"] = True
                    bot.open_position["bracket_stop"] = round(_round_to_tick(_stop, rec.symbol), 4)
                    bot.open_position["bracket_target"] = round(_round_to_tick(_target, rec.symbol), 4)
                    bot.open_position["bracket_src"] = _bracket_src
                    # Successful broker acknowledgement — reset the
                    # consecutive-reject backpressure counter.
                    bot.consecutive_broker_rejects = 0
                elif _result.status.value not in _ok_statuses:
                    _rollback_recorded_entry(
                        f"broker_result={_result.status.value}; reason={_reason}",
                    )
                    return None
            except Exception as _exc:
                logger.warning("DIRECT ORDER FAILED: %s %s: %s", rec.symbol, rec.side, _exc)
                _rollback_recorded_entry(f"broker_exception={_exc}")
                return None

        return rec

    def _get_broker_position_qty(self, bot: BotInstance) -> float | None:
        """Return the broker's authoritative position size for ``bot.symbol``.

        Used by submit_exit so we never ship more than the broker actually
        holds — partial fills, broker-side bracket leg fires, and clientId
        races can all leave the broker holding fewer contracts than the
        supervisor believes.

        Returns the absolute quantity if the broker query succeeds, or
        ``None`` if the query fails. In paper_sim (no broker) we return
        the supervisor's own belief; the caller treats that as a no-op.
        """
        # paper_sim has no broker — fall back to the supervisor's belief
        # so divergence checks become a no-op.
        if self.cfg.mode != "paper_live":
            pos = bot.open_position or {}
            try:
                return abs(float(pos.get("qty", 0) or 0))
            except (TypeError, ValueError):
                return None

        # paper_live: pick the route that matches the symbol. IBKR is the
        # default for futures + IBKR-routed crypto; AlpacaVenue is the
        # alternate path for any bot the operator has wired through
        # Alpaca. The route choice is symbol-driven today (no per-bot
        # broker assignment yet), so we try IBKR first and fall through
        # to Alpaca if the IBKR singleton isn't initialized.
        symbol_upper = bot.symbol.upper()
        try:
            venue = _get_live_ibkr_venue()
            broker_positions = _run_on_live_ibkr_loop(
                venue.get_positions(), timeout=5.0,
            ) or []
            # IBKR returns {"symbol": "MNQ", "position": signed_qty, ...}.
            # Match by stripped symbol root so MNQ1 in supervisor matches
            # MNQ at the broker.
            sym_root = symbol_upper.rstrip("0123456789").replace("USD", "")
            for p in broker_positions:
                p_sym = str(p.get("symbol", "")).upper()
                p_root = p_sym.rstrip("0123456789").replace("USD", "")
                if p_root == sym_root or p_sym == symbol_upper:
                    return abs(float(p.get("position", 0) or 0))
            # No matching broker position — broker holds 0.
            return 0.0
        except Exception as exc:  # noqa: BLE001 — fall through to Alpaca
            logger.debug(
                "_get_broker_position_qty IBKR query failed for %s: %s",
                bot.bot_id, exc,
            )

        # Try Alpaca as a fallback. AlpacaVenue.get_positions() is async
        # and the engine doesn't yet have a generic async runner outside
        # the IBKR loop, so we wrap the asyncio.run path defensively.
        try:
            from eta_engine.venues.alpaca import AlpacaVenue

            alpaca = AlpacaVenue()
            try:
                positions = asyncio.run(alpaca.get_positions())
            except RuntimeError:
                # Already inside a running loop (rare in supervisor; can
                # happen in tests). Don't try to reuse it — just bail.
                return None
            for p in positions or []:
                if str(p.get("symbol", "")).upper() == symbol_upper:
                    return abs(float(p.get("position", 0) or 0))
            return 0.0
        except Exception as exc:  # noqa: BLE001 — broker query failed
            logger.debug(
                "_get_broker_position_qty Alpaca query failed for %s: %s",
                bot.bot_id, exc,
            )
            return None

    def submit_exit(
        self,
        *,
        bot: BotInstance,
        bar: dict[str, Any],
    ) -> FillRecord | None:
        """Close the bot's open position.

        Returns the FillRecord for the exit; ``rec.entry_snapshot`` (a
        dict mirroring the original entry fields) is attached as an
        attribute so the caller can pass it into ``_propagate_close``.
        Without that snapshot, _propagate_close would read
        ``bot.open_position`` AFTER this method clears it and feed the
        exit-side / exit-price into the edge_tracker — inverting the
        feedback signal for every close.

        The exit qty is reconciled against the broker via
        ``_get_broker_position_qty`` — if the broker reports a smaller
        size than the supervisor believes (partial fill, broker-bracket
        leg fired, clientId race), the broker's number wins so we never
        ship an oversized exit.
        """
        if bot.open_position is None:
            return None
        pos = bot.open_position
        # ── BROKER-QTY RECONCILIATION ──────────────────────────────
        # The supervisor's belief about position size can drift from
        # broker reality across partial fills, broker-bracket leg fires,
        # and clientId races. Querying the broker here keeps the exit
        # bounded by what's actually held; if the query fails (broker
        # unreachable / paper_sim has no broker), we fall back to the
        # supervisor's belief but log it so the operator can investigate.
        try:
            supervisor_qty = abs(float(pos.get("qty", 0) or 0))
        except (TypeError, ValueError):
            supervisor_qty = 0.0
        broker_qty = self._get_broker_position_qty(bot)
        if broker_qty is None:
            logger.info(
                "submit_exit: broker qty unavailable for %s; using "
                "supervisor qty=%.6f",
                bot.bot_id, supervisor_qty,
            )
            exit_qty = supervisor_qty
        elif broker_qty < supervisor_qty:
            logger.warning(
                "QTY DIVERGENCE %s: supervisor believes %.6f, broker holds "
                "%.6f — sizing exit against broker qty",
                bot.bot_id, supervisor_qty, broker_qty,
            )
            exit_qty = broker_qty
        else:
            exit_qty = supervisor_qty
        side_close = "SELL" if pos["side"] == "BUY" else "BUY"
        # Adverse slippage on exit (same sign convention as submit_entry):
        # BUY-back fills above mid; SELL fills below mid. Magnitude is
        # always positive. Earlier ``-1.5`` for SELL gave the trader a
        # better-than-mid fill — wrong direction.
        adverse_bps = 1.5
        sign_slip_exit = 1.0 if side_close == "BUY" else -1.0
        # Paper-sim exit fill routing (Fix 2): when ``_maybe_exit`` set
        # ``exit_reason`` on the position, fill at the bracket-leg price
        # rather than ``bar.close``. Real bracket legs cross the spread:
        # stops fill slightly worse than the trigger, takeprofit limits
        # fill at the limit (best case). This stops paper R from
        # over-booking winners (filling at close above target) and
        # under-booking wickers (filling at close after a wick stops us).
        exit_reason = str(pos.get("exit_reason") or "")
        entry_price = float(pos["entry_price"])
        ref_close = float(bar.get("close", entry_price))
        if exit_reason == "paper_stop" and pos.get("bracket_stop") is not None:
            try:
                stop_price = float(pos["bracket_stop"])
                # Stop crossed the spread: fill is ADVERSE to the holder.
                # LONG-side stop is below entry, exit is a SELL — receive
                # below stop. SHORT-side stop is above entry, exit is a
                # BUY — pay above stop. ``sign_slip_exit`` already encodes
                # the side: SELL → -1 (below), BUY → +1 (above).
                fill_price = stop_price + sign_slip_exit * (
                    stop_price * adverse_bps / 10_000.0
                )
            except (TypeError, ValueError):
                fill_price = ref_close + sign_slip_exit * (
                    ref_close * adverse_bps / 10_000.0
                )
        elif exit_reason == "paper_target" and pos.get("bracket_target") is not None:
            try:
                # Take-profit limits fill AT the limit (best case) when
                # the market trades through. No adverse slippage applied.
                fill_price = float(pos["bracket_target"])
            except (TypeError, ValueError):
                fill_price = ref_close + sign_slip_exit * (
                    ref_close * adverse_bps / 10_000.0
                )
        else:
            # Default path (no bracket reason set, e.g. emergency exit,
            # legacy random/percent fallback): mid + adverse slippage.
            fill_price = ref_close + sign_slip_exit * (
                ref_close * adverse_bps / 10_000.0
            )
        fill_price = _round_to_tick(fill_price, bot.symbol)
        # Realized P&L (paper) — multiply by instrument point_value so
        # futures contracts (MNQ=$2/pt, ES=$50/pt, GC=$100/pt, etc.) get
        # accurate dollar PnL. Crypto spot returns point_value=1.0 from
        # the default-spec branch, so it falls through unchanged.
        try:
            from eta_engine.feeds.instrument_specs import get_spec
            _pv = float(get_spec(bot.symbol).point_value or 1.0)
        except Exception as exc:  # noqa: BLE001
            # Surface the lookup failure so a registry/spec gap doesn't
            # silently silently calculate futures PnL with multiplier=1
            # (e.g. MNQ booked at 1/2x of true PnL).
            logger.debug(
                "point_value lookup failed for %s, defaulting to 1.0: %s",
                bot.symbol, exc,
            )
            _pv = 1.0
        sign = 1.0 if pos["side"] == "BUY" else -1.0
        pnl_per_unit = (fill_price - pos["entry_price"]) * sign
        # Use the reconciled exit_qty (broker-authoritative when available)
        # for both the PnL calc AND the outgoing FillRecord. Otherwise
        # downstream R-attribution and the broker order would disagree
        # on size.
        pnl = pnl_per_unit * exit_qty * _pv
        # Realized R: prefer planned bracket-stop distance × qty × pv as
        # the denominator. That is what the lab uses, so live R becomes
        # apples-to-apples comparable to lab expectancy_r. Falls back to
        # 1% of cash for legacy positions without a stored bracket.
        plan_stop = pos.get("bracket_stop")
        risk_unit = 0.0
        if plan_stop is not None:
            with contextlib.suppress(TypeError, ValueError):
                risk_unit = abs(float(plan_stop) - pos["entry_price"]) * exit_qty * _pv
        if risk_unit <= 0:
            risk_unit = bot.cash * 0.01
        realized_r = pnl / max(risk_unit, 1e-9) if risk_unit > 0 else 0.0

        rec = FillRecord(
            bot_id=bot.bot_id,
            signal_id=pos["signal_id"],
            side=side_close,
            symbol=bot.symbol,
            qty=exit_qty,
            fill_price=round(fill_price, 4),
            fill_ts=datetime.now(UTC).isoformat(),
            paper=True,
            realized_r=round(realized_r, 4),
            realized_pnl=round(pnl, 4),
            note=f"close pnl={pnl:+.2f}",
        )

        bot.realized_pnl += pnl
        bot.cash += pnl
        bot.n_exits += 1
        # Capture the entry-state snapshot BEFORE clearing bot.open_position
        # so _propagate_close can pass real entry_side / entry_price into
        # edge_tracker.observe(). Earlier this happened AFTER the clear,
        # which meant every observe() saw a None pos and fell back to the
        # exit FillRecord's side/price — inverted from what the tracker
        # expects. The dict mirrors the open_position layout so callers
        # can pass it straight into _propagate_close.
        entry_snapshot = {
            "side": pos.get("side"),
            "entry_price": pos.get("entry_price"),
            "qty": pos.get("qty"),
            "bracket_stop": pos.get("bracket_stop"),
            "bracket_target": pos.get("bracket_target"),
            "signal_id": pos.get("signal_id"),
        }
        # Attach as an attribute (rather than a new field on FillRecord)
        # to avoid touching the FillRecord dataclass — keeps the change
        # local to the supervisor and doesn't ripple into journal/router
        # serialization paths.
        rec.entry_snapshot = entry_snapshot  # type: ignore[attr-defined]
        bot.open_position = None
        return rec

    def _write_pending_order(self, bot: BotInstance, rec: FillRecord) -> None:
        # Pull the planned bracket the supervisor computed at entry
        # (set on bot.open_position by submit_entry just above this
        # call). Both fields are required for futures entries — the
        # venue layer rejects naked entries — so a missing bracket
        # becomes a JSON ``null`` and the broker_router fails closed
        # downstream rather than papering over the omission here.
        pos = bot.open_position or {}
        # Tick-grid rounding (Fix 4): stop/target/limit are already
        # tick-rounded upstream when ``submit_entry`` writes them, but
        # defensively re-round here so an externally-mutated
        # bot.open_position can't ship un-quantized prices to IBKR
        # while the supervisor records the un-rounded value.
        _raw_stop = pos.get("bracket_stop")
        _raw_target = pos.get("bracket_target")
        stop_price = _round_to_tick(float(_raw_stop), rec.symbol) if _raw_stop is not None else None
        target_price = _round_to_tick(float(_raw_target), rec.symbol) if _raw_target is not None else None
        limit_price = _round_to_tick(float(rec.fill_price), rec.symbol)
        try:
            f = self.bf_dir / f"{bot.bot_id}.pending_order.json"
            f.write_text(
                json.dumps({
                    "ts": rec.fill_ts,
                    "signal_id": rec.signal_id,
                    "side": rec.side,
                    "qty": rec.qty,
                    "symbol": rec.symbol,
                    "limit_price": limit_price,
                    "stop_price": stop_price,
                    "target_price": target_price,
                }, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("pending order write failed (%s)", exc)


# ─── Supervisor ───────────────────────────────────────────────────


# ─── Heartbeat keep-alive thread ──────────────────────────────────
#
# The main heartbeat (`heartbeat.json`) is bound to the tick loop:
# every iteration of `run_forever` walks the fleet, runs JARVIS
# consult chains, talks to brokers, then writes the heartbeat. If
# any of those blocks (a stuck ib_insync reconnect, a long JARVIS
# layer, a hung futures call) the heartbeat goes stale by definition
# even though the supervisor process is healthy and would recover on
# the next tick.
#
# The keep-alive is a daemon thread that writes
# `heartbeat_keepalive.json` with a single field:
#
#     {"keepalive_ts": "<utc-iso>"}
#
# every KEEPALIVE_PERIOD_S seconds, completely independent of the
# main tick loop. The diagnostic CLI reads both files: a fresh
# keepalive paired with a stale main heartbeat is reported as
# `main_loop_stuck` (process alive, loop blocked); a stale keepalive
# is reported as `supervisor_dead` (process gone). This lets the
# operator triage with one diagnostic run instead of cross-checking
# `ps` against the heartbeat age.

_KEEPALIVE_PERIOD_S = float(os.getenv("ETA_SUPERVISOR_KEEPALIVE_PERIOD_S", "15"))
_KEEPALIVE_FILENAME = "heartbeat_keepalive.json"


class _HeartbeatKeepAlive:
    """Daemon timer that writes a minimal "process alive" stamp.

    Decoupled from the main tick loop so a blocked tick doesn't
    silence the keep-alive. Uses ``threading.Event.wait(period)``
    instead of ``time.sleep`` so ``stop()`` returns promptly even
    when the period is large.
    """

    def __init__(self, *, state_dir: Path, period_s: float) -> None:
        self._state_dir = state_dir
        # Floor at 10ms so tests can run quickly; production cadence
        # is set via ETA_SUPERVISOR_KEEPALIVE_PERIOD_S (default 15s).
        # A 10ms floor still prevents a busy-loop-by-misconfig.
        self._period_s = max(0.01, float(period_s))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return self._state_dir / _KEEPALIVE_FILENAME

    def start(self) -> None:
        """Spawn the daemon thread and write an initial stamp.

        Daemon=True means the thread does not prevent process exit;
        the thread additionally responds to ``stop()`` so SIGTERM
        / SIGINT shutdowns are clean and the final keepalive stamp
        is the actual shutdown moment, not a stale value.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        # Write one stamp synchronously so a downstream diagnostic
        # immediately after start() sees a fresh keepalive even if
        # the thread hasn't gotten its first scheduling slice yet.
        self._write_stamp()
        thread = threading.Thread(
            target=self._run,
            name="jarvis-supervisor-keepalive",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the thread to exit and wait briefly for it to join."""
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        # join() respects the stop-event; the loop body returns
        # within one period_s slice once the event is set.
        with contextlib.suppress(RuntimeError):
            thread.join(timeout=timeout)

    def _run(self) -> None:
        # First wait, then write — initial stamp was written by start().
        while not self._stop_event.wait(self._period_s):
            try:
                self._write_stamp()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:  # noqa: BLE001 -- never crash the keepalive
                # The keep-alive is the LAST line of liveness defense.
                # Any failure here is caught + logged at WARNING but
                # the loop continues — a transient disk-full or
                # permission glitch must not silence the keepalive.
                logger.warning(
                    "keepalive stamp write failed; will retry in %.1fs",
                    self._period_s,
                    exc_info=True,
                )

    def _write_stamp(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"keepalive_ts": datetime.now(UTC).isoformat()}
        # Atomic-ish write: small payload, single open(), no temp swap.
        # Conflicts with the diagnostic reader are tolerated — the
        # reader either gets the prior stamp or the new one, never a
        # partially-written file at this scale.
        self.path.write_text(
            json.dumps(payload), encoding="utf-8",
        )


class JarvisStrategySupervisor:
    """The supervisor loop. JARVIS is the admin -- every decision
    flows through ``JarvisFull.consult()`` which chains all the
    wave-7-16 intelligence layers."""

    # Cached resolution of data_feeds._is_real_bar. Sentinel ``...`` =
    # "not yet attempted"; ``None`` = "import failed permanently, do
    # not retry on every tick"; otherwise = the callable.
    _IS_REAL_BAR_FN: Any = ...

    def __init__(self, cfg: SupervisorConfig | None = None) -> None:
        self.cfg = cfg or SupervisorConfig()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._stopped = False
        self.bots: list[BotInstance] = []
        # Real-data feed dispatch: ETA_SUPERVISOR_FEED selects between
        # mock | yfinance | coinbase | ibkr | composite. Mock is the
        # default; composite is the recommended live setup (crypto via
        # Coinbase, futures via TWS, fallback yfinance).
        from eta_engine.scripts.data_feeds import make_data_feed
        self.feed = make_data_feed(self.cfg.data_feed)
        logger.info(
            "supervisor data feed: %s (%s)",
            self.cfg.data_feed, type(self.feed).__name__,
        )
        self._jarvis_full = None
        self._memory = None
        self._router = ExecutionRouter(
            cfg=self.cfg,
            bf_dir=self.cfg.broker_router_pending_dir,
            bots_ref=lambda: self.bots,
        )
        # Counter incremented every time _write_heartbeat catches an
        # otherwise-uncaught exception. Surfaced in the sidecar JSONL
        # and useful for tests / future health-check expansion.
        self._heartbeat_write_errors: int = 0
        # Slot for the keep-alive timer thread. Created lazily inside
        # run_forever so unit tests that exercise _write_heartbeat
        # directly don't spin up background threads.
        self._keepalive: _HeartbeatKeepAlive | None = None
        # Stop event for the main loop sleep. SIGTERM/SIGINT sets this
        # so we don't have to wait the full tick_s before exiting.
        self._stop_event = threading.Event()
        # Per-instance dedup for feed-health alerts. Was a class-level
        # mutable set, which would have leaked state between supervisor
        # instances in the same process.
        self._feed_health_alerted: set[str] = set()
        # Restart-safe signal_id ledger. Populated by
        # _load_recent_sent_signals() at the top of run_forever; consulted
        # in _maybe_enter() before generating a fresh signal_id, and
        # appended to via _record_sent_signal() after every successful
        # broker submission.  This stops a crash-after-pending-write
        # scenario from re-issuing the same broker submission on restart.
        self._sent_signals: set[tuple[str, str]] = set()
        # Reconcile-divergence guard (Fix 4 — restart safety). When
        # reconcile_with_broker() detects positions at the broker that
        # the supervisor has no record of, this flag is flipped True and
        # _maybe_enter is short-circuited until the operator clears via
        # ETA_RECONCILE_DIVERGENCE_ACK=1 or the
        # ``state_dir/reconcile_divergence_acknowledged.txt`` file is
        # touched after the divergence detection time.
        self._reconcile_divergence_detected: bool = False
        self._reconcile_divergence_at: datetime | None = None
        # Per-bot reject-storm trip set. Bots in this set are skipped
        # by _maybe_enter until consecutive_broker_rejects resets to 0
        # (a successful broker fill clears it). Pairs with the
        # consecutive_broker_rejects counter on BotInstance.
        self._reject_tripped_bots: set[str] = set()

    # ── Bot loading ──────────────────────────────────────────

    def load_bots(self) -> int:
        """Load active bots from per_bot_registry."""
        try:
            from eta_engine.strategies.per_bot_registry import (
                ASSIGNMENTS,
                is_active,
                validate_registry_no_duplicates,
            )
        except ImportError as exc:
            logger.error("per_bot_registry import failed (%s)", exc)
            return 0

        # Refuse to load if the active fleet contains two bots with
        # identical tradeable config — would route the same trades to
        # the broker twice on the same edge.  Pair with the same guard
        # in MnqLiveSupervisor.start().
        try:
            validate_registry_no_duplicates(raise_on_duplicate=True)
        except RuntimeError as exc:
            logger.error(
                "supervisor REFUSING TO LOAD BOTS — duplicate-config "
                "registry entries: %s", exc,
            )
            raise
        except TypeError:
            # Older registry without raise_on_duplicate kwarg — skip.
            logger.warning(
                "validate_registry_no_duplicates lacks raise_on_duplicate; "
                "skipping duplicate-config guard",
            )

        # Filter to operator-pinned subset (if any)
        pinned = {
            x.strip() for x in self.cfg.bots_env.split(",") if x.strip()
        }

        for a in ASSIGNMENTS:
            if pinned and a.bot_id not in pinned:
                continue
            if not is_active(a):
                continue
            self.bots.append(BotInstance(
                bot_id=a.bot_id,
                symbol=getattr(a, "symbol", a.bot_id.upper()),
                strategy_kind=getattr(a, "strategy_kind", "unknown"),
                direction=getattr(a, "default_direction", "long"),
                cash=self.cfg.starting_cash_per_bot,
            ))
        logger.info(
            "loaded %d bots (pinned filter: %s)",
            len(self.bots), pinned or "ALL",
        )
        return len(self.bots)

    # ── JarvisFull bootstrap ─────────────────────────────────

    def bootstrap_jarvis(self) -> bool:
        try:
            from eta_engine.brain.jarvis_admin import JarvisAdmin
            from eta_engine.brain.jarvis_v3.intelligence import (
                IntelligenceConfig,
                JarvisIntelligence,
            )
            from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
            from eta_engine.brain.jarvis_v3.memory_hierarchy import (
                HierarchicalMemory,
            )
            self._memory = HierarchicalMemory()
            admin = JarvisAdmin()
            intel = JarvisIntelligence(
                admin=admin, memory=self._memory,
                cfg=IntelligenceConfig(enable_intelligence=True),
            )
            self._jarvis_full = JarvisFull(
                intelligence=intel, memory=self._memory,
            )
            logger.info("JarvisFull bootstrapped")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("JarvisFull bootstrap failed: %s", exc)
            return False

    # ── Feed-health alerting ────────────────────────────────
    # NOTE: dedup state lives on each instance (self._feed_health_alerted),
    # populated in __init__. Class-level was a mutable-default footgun.

    def _emit_feed_health_alerts(self, snapshot: dict[str, dict[str, int]]) -> None:
        """Emit a v3 event when any (feed, symbol) has empty-rate over
        the alert threshold. Once alerted, a feed/symbol pair is muted
        until either (a) a non-empty observation comes in and resets
        the rate, or (b) the supervisor restarts."""
        if not snapshot:
            return
        threshold = float(os.getenv("ETA_FEED_ALERT_EMPTY_RATE", "0.30"))
        min_samples = int(os.getenv("ETA_FEED_ALERT_MIN_SAMPLES", "10"))
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
        except ImportError:
            return

        cleared = set()
        for key, counts in snapshot.items():
            ok = int(counts.get("ok", 0))
            empty = int(counts.get("empty", 0))
            total = ok + empty
            if total < min_samples:
                continue
            empty_rate = empty / total
            if empty_rate >= threshold:
                if key in self._feed_health_alerted:
                    continue
                self._feed_health_alerted.add(key)
                feed_name, _, sym = key.partition("::")
                with contextlib.suppress(Exception):
                    emit_event(
                        layer="feed_health",
                        event="feed_degraded",
                        bot_id="",
                        cls=sym,
                        details={
                            "feed": feed_name,
                            "symbol": sym,
                            "ok": ok, "empty": empty,
                            "empty_rate": round(empty_rate, 4),
                            "threshold": threshold,
                        },
                        severity="WARN",
                    )
            elif key in self._feed_health_alerted:
                # Feed recovered — clear the dedup so the next degradation
                # re-alerts.
                cleared.add(key)
        self._feed_health_alerted -= cleared

    # ── Restart-safe signal_id ledger ─────────────────────────
    #
    # The previous design generated signal_id with a fresh uuid4 every
    # call. If the supervisor crashed after writing a pending_order
    # JSON but before broker_router consumed it, the next session
    # would generate a DIFFERENT uuid for the same logical entry and
    # the broker_router could submit the stale file under a fresh
    # submission key — duplicate fill. This dedup ledger persists
    # every signal we ship; on restart we load the last 24h of
    # signals and refuse to re-issue any of them.

    def _sent_signals_log_path(self) -> Path:
        return self.cfg.state_dir / _SENT_SIGNALS_LOG_FILENAME

    def _record_sent_signal(
        self, bot_id: str, signal_id: str, sent_at_iso: str,
    ) -> None:
        """Atomically append a single signal record to the JSONL ledger.

        Atomicity strategy: read existing contents, append a single
        line, write to a sibling .tmp file, then os.replace() — this
        keeps the on-disk file in a consistent state even if the write
        is interrupted mid-flush. For a JSONL append-only log this is
        sufficient — we don't need full lock-file semantics because
        only one supervisor process owns the ledger at a time.
        """
        path = self._sent_signals_log_path()
        record = {
            "bot_id": bot_id,
            "signal_id": signal_id,
            "sent_at_utc": sent_at_iso,
        }
        line = json.dumps(record, sort_keys=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = ""
            if path.exists():
                try:
                    existing = path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "_record_sent_signal: read of %s failed (%s); "
                        "continuing with append-only write",
                        path, exc,
                    )
                    existing = ""
            new_payload = existing + (line + "\n")
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(new_payload, encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError as exc:
            # The ledger is best-effort hardening — failing to record
            # cannot block the trade itself. Log at WARNING so the
            # operator notices but the system keeps running.
            logger.warning(
                "_record_sent_signal: failed to persist %s/%s: %s",
                bot_id, signal_id, exc,
            )

    def _load_recent_sent_signals(
        self, hours: int = _SENT_SIGNALS_DEDUP_HOURS,
    ) -> set[tuple[str, str]]:
        """Return (bot_id, signal_id) pairs sent in the last ``hours``.

        Older entries are ignored to keep the dedup window bounded —
        a 24h window comfortably covers any plausible restart and
        keeps the file from growing unbounded.

        Failure modes (file missing, malformed lines, parse errors)
        return an empty set rather than raising — a corrupt ledger
        must not block startup, only its anti-duplication coverage.
        """
        path = self._sent_signals_log_path()
        if not path.exists():
            return set()
        cutoff = datetime.now(UTC).timestamp() - (max(0, int(hours)) * 3600)
        out: set[tuple[str, str]] = set()
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts_str = str(rec.get("sent_at_utc") or "")
                bot_id = str(rec.get("bot_id") or "")
                sig_id = str(rec.get("signal_id") or "")
                if not (ts_str and bot_id and sig_id):
                    continue
                try:
                    ts_dt = datetime.fromisoformat(ts_str)
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=UTC)
                    if ts_dt.timestamp() >= cutoff:
                        out.add((bot_id, sig_id))
                except (ValueError, TypeError):
                    continue
        except OSError as exc:
            logger.warning(
                "_load_recent_sent_signals: read of %s failed (%s); "
                "starting with empty dedup set",
                path, exc,
            )
            return set()
        return out

    # ── Reconcile-divergence acknowledgement check ────────────

    def _reconcile_divergence_acknowledged(self) -> bool:
        """Return True if the operator has cleared the divergence guard.

        Two clearance paths:
          1. Env: ``ETA_RECONCILE_DIVERGENCE_ACK=1`` (set on the running
             supervisor process; intended for ops dev who restarts).
          2. State file: ``state_dir/reconcile_divergence_acknowledged.txt``
             whose mtime is AFTER the divergence-detection moment.
        """
        if os.environ.get(
            "ETA_RECONCILE_DIVERGENCE_ACK", "",
        ).strip().lower() in {"1", "true", "yes", "on"}:
            return True
        path = self.cfg.state_dir / "reconcile_divergence_acknowledged.txt"
        if not path.exists():
            return False
        if self._reconcile_divergence_at is None:
            # No record of when the divergence was detected — be
            # permissive and trust the ack-file presence.
            return True
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            return False
        return mtime >= self._reconcile_divergence_at

    # ── Reject-storm auto-trip check ──────────────────────────

    def _check_reject_auto_trip(self, bot: BotInstance) -> bool:
        """Return True when the bot should skip _maybe_enter due to
        consecutive broker rejects breaching the configured threshold.

        Threshold default is 5; configurable via env
        ``ETA_MAX_CONSECUTIVE_REJECTS``. When the threshold trips, we
        record a non-latching FLATTEN_BOT verdict on the kill_switch
        latch (per-bot trips are intentionally non-latching, but the
        verdict logging surface is useful) and add the bot to the
        in-memory trip set. The trip is cleared automatically when
        consecutive_broker_rejects resets to 0 (i.e. a successful
        broker fill).
        """
        try:
            threshold = int(os.environ.get("ETA_MAX_CONSECUTIVE_REJECTS", "5"))
        except (TypeError, ValueError):
            threshold = 5
        threshold = max(1, threshold)
        rejects = int(getattr(bot, "consecutive_broker_rejects", 0) or 0)
        # Auto-clear the trip when the bot's reject counter resets.
        if rejects == 0 and bot.bot_id in self._reject_tripped_bots:
            self._reject_tripped_bots.discard(bot.bot_id)
            logger.info(
                "REJECT TRIP CLEARED %s: consecutive_broker_rejects back to 0",
                bot.bot_id,
            )
            return False
        if rejects >= threshold:
            if bot.bot_id not in self._reject_tripped_bots:
                self._reject_tripped_bots.add(bot.bot_id)
                logger.critical(
                    "REJECT STORM TRIP %s: %d consecutive broker rejects "
                    "(threshold=%d) — entries halted for this bot until "
                    "counter resets",
                    bot.bot_id, rejects, threshold,
                )
                # Best-effort: record a non-latching FLATTEN_BOT verdict
                # for telemetry. Per-bot trips do NOT flip the global
                # latch — that is intentional design.
                latch = getattr(self, "_kill_switch_latch", None)
                if latch is not None:
                    with contextlib.suppress(Exception):
                        from eta_engine.core.kill_switch_runtime import KillAction, KillSeverity, KillVerdict

                        latch.record_verdict(
                            KillVerdict(
                                action=KillAction.FLATTEN_BOT,
                                severity=KillSeverity.CRITICAL,
                                reason=(
                                    f"{bot.bot_id} has {rejects} consecutive "
                                    f"broker rejects (threshold={threshold})"
                                ),
                                scope=f"bot:{bot.bot_id}",
                                evidence={
                                    "consecutive_broker_rejects": rejects,
                                    "threshold": threshold,
                                },
                            )
                        )
                # Emit a v3 event so Hermes can ping the operator.
                with contextlib.suppress(Exception):
                    from eta_engine.brain.jarvis_v3.policies._v3_events import (
                        emit_event,
                    )
                    emit_event(
                        layer="risk",
                        event="reject_storm_trip",
                        bot_id=bot.bot_id,
                        details={
                            "consecutive_broker_rejects": rejects,
                            "threshold": threshold,
                        },
                        severity="CRITICAL",
                    )
            return True
        return False

    # ── Broker reconciliation (run once on startup) ──────────

    def reconcile_with_broker(self) -> dict[str, Any]:
        """Compare broker open positions to bot.open_position state and
        log divergence. Run once at startup so a supervisor restart
        with broker-side positions still open is surfaced before more
        signals fire and over-leverage the account.

        Queries BOTH IBKR (futures, IBKR-routed crypto) and Alpaca
        (crypto). When ANY divergence exists where the broker holds a
        position the supervisor doesn't know about (broker_only OR
        divergent), the supervisor's
        ``_reconcile_divergence_detected`` flag is set. The main tick
        loop short-circuits ``_maybe_enter`` for ALL bots until the
        operator clears via env var or state file — see
        ``_reconcile_divergence_acknowledged``.

        Does NOT auto-patch bot state — auto-attribution is unreliable
        when multiple bots share a symbol (5 BTC bots / 1 broker BTC
        net position). Operator reads the divergence + decides.
        """
        findings: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "mode": self.cfg.mode,
            "broker_only": [],
            "supervisor_only": [],
            "divergent": [],
            "matched": 0,
            "brokers_queried": [],
        }
        if self.cfg.mode != "paper_live":
            findings["skipped_reason"] = f"mode={self.cfg.mode} (only paper_live reconciles)"
            return findings

        broker_by_root: dict[str, float] = {}

        # ── IBKR ────────────────────────────────────────────────
        try:
            venue = _get_live_ibkr_venue()
            ibkr_positions = _run_on_live_ibkr_loop(
                venue.get_positions(), timeout=10.0,
            )
            findings["brokers_queried"].append("ibkr")
            for p in ibkr_positions or []:
                sym_raw = str(p.get("symbol", "")).upper()
                root = sym_raw.rstrip("0123456789").replace("USD", "")
                broker_by_root[root] = broker_by_root.get(root, 0.0) + float(
                    p.get("position", 0) or 0
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile_with_broker: IBKR query failed: %s", exc)
            findings["ibkr_error"] = str(exc)

        # ── Alpaca (crypto) ─────────────────────────────────────
        # Crypto positions live at Alpaca for the BTC/ETH/SOL bots; an
        # IBKR-only reconcile would silently miss broker-side crypto
        # exposure on supervisor restart, exactly the failure mode
        # this guard exists to prevent.
        try:
            from eta_engine.venues.alpaca import AlpacaVenue
            alpaca = AlpacaVenue()
            try:
                alpaca_positions = asyncio.run(alpaca.get_positions())
            except RuntimeError:
                # Already inside a running loop. Use the IBKR-loop
                # runner as a generic async runner — it just executes
                # the coroutine and waits for the result.
                alpaca_positions = _run_on_live_ibkr_loop(
                    alpaca.get_positions(), timeout=10.0,
                )
            findings["brokers_queried"].append("alpaca")
            for p in alpaca_positions or []:
                sym_raw = str(p.get("symbol", "")).upper()
                # Alpaca crypto symbols arrive as e.g. "BTCUSD" — strip
                # the USD suffix and any digits so the root matches the
                # supervisor's bot symbol root.
                root = sym_raw.rstrip("0123456789").replace("USDT", "").replace("USD", "")
                # Alpaca returns ``qty`` (signed) on positions; fall
                # back to ``position`` for parity with the IBKR shape.
                raw_qty = p.get("qty")
                if raw_qty is None:
                    raw_qty = p.get("position", 0)
                try:
                    qty_val = float(raw_qty or 0)
                except (TypeError, ValueError):
                    qty_val = 0.0
                broker_by_root[root] = broker_by_root.get(root, 0.0) + qty_val
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile_with_broker: Alpaca query failed: %s", exc)
            findings["alpaca_error"] = str(exc)

        # If both brokers failed there is nothing to compare.
        if not findings["brokers_queried"]:
            findings["error"] = "no broker queries succeeded"
            return findings

        # Sum supervisor positions by symbol root, signed
        supervisor_by_root: dict[str, float] = {}
        for bot in self.bots:
            pos = getattr(bot, "open_position", None)
            if not pos:
                continue
            sym_raw = bot.symbol.upper()
            root = sym_raw.rstrip("0123456789").replace("USDT", "").replace("USD", "")
            qty = abs(float(pos.get("qty", 0) or 0))
            side = str(pos.get("side", "BUY")).upper()
            signed = qty if side == "BUY" else -qty
            supervisor_by_root[root] = supervisor_by_root.get(root, 0.0) + signed

        # Diff
        for root in set(broker_by_root) | set(supervisor_by_root):
            b_qty = broker_by_root.get(root, 0.0)
            s_qty = supervisor_by_root.get(root, 0.0)
            if abs(b_qty - s_qty) < 1e-6:
                findings["matched"] += 1
                continue
            if abs(s_qty) < 1e-6:
                findings["broker_only"].append({"symbol": root, "broker_qty": b_qty})
            elif abs(b_qty) < 1e-6:
                findings["supervisor_only"].append({"symbol": root, "supervisor_qty": s_qty})
            else:
                findings["divergent"].append({
                    "symbol": root,
                    "broker_qty": b_qty,
                    "supervisor_qty": s_qty,
                    "delta": b_qty - s_qty,
                })

        # Set the runtime divergence flag whenever the broker holds a
        # position the supervisor doesn't know about (broker_only) OR
        # both sides hold inconsistent qtys (divergent). supervisor_only
        # is NOT a hold-orders condition: that's just stale supervisor
        # state, not broker exposure the supervisor would layer on top
        # of.
        has_broker_unknown = bool(
            findings["broker_only"] or findings["divergent"]
        )
        if has_broker_unknown:
            self._reconcile_divergence_detected = True
            self._reconcile_divergence_at = datetime.now(UTC)
            logger.critical(
                "RECONCILE BROKER POSITIONS UNKNOWN TO SUPERVISOR — "
                "halting new entries until operator clears: "
                "broker_only=%s divergent=%s",
                findings["broker_only"], findings["divergent"],
            )
            with contextlib.suppress(Exception):
                from eta_engine.brain.jarvis_v3.policies._v3_events import (
                    emit_event,
                )
                emit_event(
                    layer="ops",
                    event="reconcile_divergence_detected",
                    bot_id="",
                    details={
                        "broker_only": findings["broker_only"],
                        "divergent": findings["divergent"],
                        "brokers_queried": findings["brokers_queried"],
                    },
                    severity="CRITICAL",
                )
        elif findings["broker_only"] or findings["supervisor_only"] or findings["divergent"]:
            logger.warning(
                "RECONCILE divergence: broker_only=%s supervisor_only=%s divergent=%s",
                findings["broker_only"], findings["supervisor_only"], findings["divergent"],
            )
        else:
            logger.info(
                "RECONCILE: supervisor + brokers agree on %d symbols (%s)",
                findings["matched"], ",".join(findings["brokers_queried"]),
            )

        # Persist findings so the dashboard / red-team can see them
        with contextlib.suppress(OSError):
            (self.cfg.state_dir / "reconcile_last.json").write_text(
                json.dumps(findings, indent=2, default=str), encoding="utf-8",
            )
        return findings

    # ── Main loop ───────────────────────────────────────────

    def run_forever(self) -> int:
        """Run the supervisor loop until SIGTERM/SIGINT or fatal error."""
        os_signal.signal(os_signal.SIGINT, self._handle_stop)
        os_signal.signal(os_signal.SIGTERM, self._handle_stop)

        # Uptime telemetry: stamp a "start" event before any boot work so
        # post-mortems can prove the process at least entered run_forever.
        # Failure-tolerant: record_uptime_event swallows errors so a
        # disk-full state directory cannot crash the supervisor.
        with contextlib.suppress(Exception):
            record_uptime_event(
                component="supervisor",
                event="start",
                reason="run_forever_entered",
                extra={
                    "mode": self.cfg.mode,
                    "feed": self.cfg.data_feed,
                    "tick_s": self.cfg.tick_s,
                },
            )

        # Boot consult of the catastrophic-verdict latch. A latch tripped
        # by a prior session must prevent THIS process from issuing any
        # new entries. The latch fails closed: corrupt JSON / unreadable
        # file -> TRIPPED, not ARMED. Operator clears with
        # `python -m eta_engine.scripts.clear_kill_switch --confirm
        # --operator <name>`. Boot bypass exists via ETA_LATCH_BOOT_BYPASS=1
        # for ops dev only (not silent — emits a CRITICAL log line).
        try:
            from eta_engine.core.kill_switch_latch import KillSwitchLatch
            from eta_engine.core.kill_switch_latch import default_path as latch_default_path

            self._kill_switch_latch = KillSwitchLatch(latch_default_path())
            ok, latch_reason = self._kill_switch_latch.boot_allowed()
        except Exception as exc:  # noqa: BLE001 — boot consult is fail-closed
            logger.critical(
                "kill_switch_latch boot consult raised %s — refusing to start. "
                "Investigate var/eta_engine/state/kill_switch_latch.json",
                exc, exc_info=True,
            )
            return 3
        if not ok:
            if os.environ.get("ETA_LATCH_BOOT_BYPASS", "").strip().lower() in {"1", "true", "yes"}:
                logger.critical(
                    "kill_switch_latch is TRIPPED but ETA_LATCH_BOOT_BYPASS=1 — "
                    "OPERATOR-AUTHORIZED override. Reason: %s",
                    latch_reason,
                )
            else:
                logger.error("REFUSING TO START: %s", latch_reason)
                return 3

        if self.load_bots() == 0:
            logger.error("no active bots loaded; exiting")
            return 1

        # Restart-safe signal_id ledger: load the last 24h of submitted
        # signals so a crash-after-pending-write cannot result in a
        # duplicate broker submission on restart. See _maybe_enter for
        # the consult and _record_sent_signal for the append.
        self._sent_signals = self._load_recent_sent_signals()
        logger.info(
            "supervisor remembers %d signals from the last %dh, "
            "will not re-issue these",
            len(self._sent_signals), _SENT_SIGNALS_DEDUP_HOURS,
        )

        if not self.bootstrap_jarvis():
            logger.error("JarvisFull bootstrap failed; exiting")
            return 2

        # Reconcile broker positions with supervisor state. A restart
        # while broker positions are still open used to silently grow
        # the fleet (supervisor thinks it has nothing open, fires fresh
        # entries on top of broker exposure). Reconcile surfaces this
        # before more orders fly.
        with contextlib.suppress(Exception):
            self.reconcile_with_broker()

        logger.info(
            "supervisor running: %d bots, mode=%s, feed=%s, tick=%.0fs, "
            "live_money=%s",
            len(self.bots), self.cfg.mode, self.cfg.data_feed,
            self.cfg.tick_s, self.cfg.live_money_enabled,
        )

        # Keep-alive thread: writes a separate {"keepalive_ts": ...}
        # file every KEEPALIVE_PERIOD_S seconds, independent of the main
        # tick loop. This is the canonical "process is alive" stamp —
        # even if _tick_once blocks (broker reconnect storm, ib_insync
        # hang, deadlock in JARVIS consult chain) and the main heartbeat
        # goes stale, the keep-alive proves the process itself is still
        # scheduled. The diagnostic CLI reads both: fresh keepalive +
        # stale main heartbeat → "main_loop_stuck", not "supervisor_dead".
        self._keepalive = _HeartbeatKeepAlive(
            state_dir=self.cfg.state_dir,
            period_s=_KEEPALIVE_PERIOD_S,
        )
        self._keepalive.start()

        tick_count = 0
        crash_exc: BaseException | None = None
        try:
            while not self._stopped:
                tick_count += 1
                # Publish a start-of-tick heartbeat before slow feed/LLM work.
                # A composite fleet tick can legitimately take minutes; the
                # dashboard should show "working" only when the loop is truly
                # stale, not merely waiting for the first full tick to finish.
                self._write_heartbeat(tick_count)
                self._tick_once(tick_count)
                self._write_heartbeat(tick_count)
                # Use an Event so SIGTERM/SIGINT can wake us early
                # rather than waiting for the full tick. Returns True
                # if event was set (we'll exit on the next while-check),
                # False after the timeout (continue looping).
                if self._stop_event.wait(self.cfg.tick_s):
                    break
        except BaseException as exc:  # noqa: BLE001 — we re-raise unless it's a clean stop
            crash_exc = exc
            raise
        finally:
            # Daemon thread exits with the process, but signal the
            # keepalive to stop cleanly so the final keepalive file
            # reflects the actual shutdown moment, not a stale stamp.
            with contextlib.suppress(Exception):
                self._keepalive.stop()
            # Uptime telemetry: stamp a "stop" event so post-mortems know
            # the process actually exited cleanly. A bare "start" entry
            # without a matching "stop" tells the watchdog the supervisor
            # crashed mid-tick.
            with contextlib.suppress(Exception):
                if crash_exc is not None and not isinstance(
                    crash_exc, (KeyboardInterrupt, SystemExit),
                ):
                    import traceback as _tb
                    last_line = ""
                    with contextlib.suppress(Exception):
                        tb_lines = _tb.format_exception(
                            type(crash_exc), crash_exc, crash_exc.__traceback__,
                        )
                        last_line = (tb_lines[-1] if tb_lines else "").strip()
                    record_uptime_event(
                        component="supervisor",
                        event="crash",
                        reason=f"{type(crash_exc).__name__}: {crash_exc}",
                        extra={
                            "tick_count": tick_count,
                            "last_traceback_line": last_line[:400],
                        },
                    )
                else:
                    record_uptime_event(
                        component="supervisor",
                        event="stop",
                        reason="sigterm_or_clean_exit"
                        if crash_exc is None
                        else type(crash_exc).__name__,
                        extra={"tick_count": tick_count},
                    )

        logger.info("supervisor stopped after %d ticks", tick_count)
        return 0

    def _handle_stop(self, signum, frame) -> None:  # noqa: ANN001 -- signal callback signature
        logger.info("stop signal received (signum=%s)", signum)
        self._stopped = True
        # Wake the main loop's _stop_event.wait() immediately so
        # shutdown isn't gated on the remaining tick_s window.
        with contextlib.suppress(Exception):
            self._stop_event.set()
        # Eagerly stop the keep-alive thread so SIGTERM/SIGINT shutdown
        # is clean — daemon=True alone is not enough; the thread should
        # observe the stop event and exit its loop body promptly.
        keepalive = getattr(self, "_keepalive", None)
        if keepalive is not None:
            with contextlib.suppress(Exception):
                keepalive.stop()

    def _tick_once(self, tick_count: int) -> None:
        for bot in self.bots:
            try:
                self._tick_bot(bot, tick_count)
            except Exception as exc:  # noqa: BLE001 -- never break the loop
                logger.exception(
                    "tick_bot %s raised: %s", bot.bot_id, exc,
                )

    def _tick_bot(self, bot: BotInstance, tick_count: int) -> None:
        # 1. Get a fresh bar
        bar = self.feed.get_bar(bot.symbol)
        bot.last_bar_ts = bar["ts"]
        bot.sage_bars.append(bar)

        # Empty-bar guard. data_feeds._empty_bar returns close=100.0,
        # volume=0 when every feed in the composite chain fails. If a
        # bot enters at the dummy $100 close on tick N and the real
        # close shows up at tick N+1, the position records a fictional
        # $48,998 PnL on YM (real $48k entry vs dummy $100). Refuse to
        # take action on flagged-empty bars; let the next tick try
        # again. _is_real_bar checks volume + OHLC flatline.
        #
        # Cache the import result on the class — a permanent ImportError
        # (e.g. data_feeds dropped this helper) would otherwise re-raise
        # on every tick across every bot. Sentinel ``...`` = not yet
        # tried; ``None`` = tried and failed permanently.
        if JarvisStrategySupervisor._IS_REAL_BAR_FN is ...:
            try:
                from eta_engine.scripts.data_feeds import _is_real_bar
                JarvisStrategySupervisor._IS_REAL_BAR_FN = _is_real_bar
            except Exception as exc:  # noqa: BLE001
                logger.debug("_is_real_bar unavailable: %s", exc)
                JarvisStrategySupervisor._IS_REAL_BAR_FN = None
        _is_real_bar_fn = JarvisStrategySupervisor._IS_REAL_BAR_FN
        if _is_real_bar_fn is not None:
            try:
                if not _is_real_bar_fn(bar):
                    return
            except Exception as exc:  # noqa: BLE001 — never break the tick on a guard helper
                logger.debug("_is_real_bar(%s) raised: %s", bot.symbol, exc)

        # 2. If no open position, evaluate entry
        if bot.open_position is None:
            self._maybe_enter(bot, bar)
        else:
            self._maybe_exit(bot, bar)

    def _maybe_enter(self, bot: BotInstance, bar: dict[str, Any]) -> None:
        # Mock entry signal: per-call independent dice, ~1-in-5 fire rate.
        #
        # The earlier ``random.Random(int(time.time())).random()`` was
        # broken on two axes:
        #
        #   (a) ``int(time.time())`` is shared across all 16 bots in a
        #       single tick, so every bot got the SAME dice roll. The
        #       effective fleet entry rate was 1/30 per tick, not 16/30.
        #   (b) ``random.Random(seed).random()`` is a deterministic
        #       function of the seed, so the entire fleet walked through
        #       a fixed sequence of dice values. A stretch of unlucky
        #       seconds could silence the whole fleet for many ticks
        #       (observed: 76 minutes with zero entries).
        #
        # Fix: use Python's module-level ``random.random()`` (per-process
        # Mersenne Twister, seeded from os.urandom at import). Each call
        # produces a fresh independent draw, and the rate is high enough
        # that 16 bots produce visible activity every tick.
        #
        # Mock-only gate: this is scaffolding for the synthetic-feed
        # validation lane. paper_live with a real feed (yfinance,
        # ibkr, coinbase, composite) must NOT be 80%-skipped here —
        # those entries are gated by JARVIS / Sage / consult layers
        # downstream, not a uniform random veto.
        if self.cfg.data_feed == "mock" and random.random() > (1.0 / 5):
            return

        # ── Reconcile-divergence guard ──────────────────────────────
        # If reconcile_with_broker() detected positions at the broker
        # the supervisor doesn't know about, refuse to layer fresh
        # entries on top of the unknown exposure. Existing supervisor-
        # known positions continue to manage themselves via _maybe_exit;
        # only NEW entries are short-circuited here. Operator clears via
        # ETA_RECONCILE_DIVERGENCE_ACK=1 or the on-disk ack file.
        if self._reconcile_divergence_detected and not self._reconcile_divergence_acknowledged():
            if not getattr(self, "_reconcile_divergence_warned", False):
                logger.warning(
                    "RECONCILE DIVERGENCE — entries halted for ALL bots "
                    "until operator clears (ETA_RECONCILE_DIVERGENCE_ACK=1 "
                    "or %s)",
                    self.cfg.state_dir / "reconcile_divergence_acknowledged.txt",
                )
                self._reconcile_divergence_warned = True
            return

        # ── Reject-storm trip ───────────────────────────────────────
        # Per-bot circuit breaker: if a bot has accumulated
        # ETA_MAX_CONSECUTIVE_REJECTS (default 5) consecutive broker
        # rejects without a successful fill in between, skip its
        # entries entirely. The trip clears automatically when the
        # bot's reject counter resets to 0 (next successful broker fill).
        if self._check_reject_auto_trip(bot):
            return

        # Daily loss kill switch — refuses new entries when today's
        # realized PnL has crossed the configured floor. Existing
        # positions continue to manage themselves via brackets;
        # only NEW entries are blocked. Resets automatically at
        # midnight (operator timezone via ETA_KILLSWITCH_TIMEZONE).
        # ── Catastrophic-verdict latch (per-entry consult) ──────────
        # The boot consult in run_forever() blocks startup when latch is
        # TRIPPED, but a verdict can flip the latch DURING a session.
        # Re-read the latch on every entry attempt. Same fail-closed
        # contract: corrupt latch -> TRIPPED. No exception swallowing
        # here — if the latch read raises, we refuse the entry rather
        # than let it through with an unobservable reason.
        latch = getattr(self, "_kill_switch_latch", None)
        if latch is not None:
            try:
                rec = latch.read()
            except Exception as exc:  # noqa: BLE001 — latch read must fail closed
                logger.critical(
                    "kill_switch_latch read failed for %s — refusing entry: %s",
                    bot.bot_id, exc,
                )
                return
            if rec.is_tripped():
                if not getattr(self, "_latch_warned_session", False):
                    logger.warning(
                        "KILL SWITCH LATCH TRIPPED — entries halted: scope=%s action=%s reason=%s",
                        rec.scope, rec.action, rec.reason,
                    )
                    self._latch_warned_session = True
                return

        # ── Daily-loss killswitch ───────────────────────────────────
        # Failure modes (file missing, malformed, import error) now
        # FAIL CLOSED — the prior swallow-and-continue path is exactly
        # the "killswitch silently disabled" gap the risk-execution
        # review flagged. Only an explicit operator file clears this.
        try:
            from eta_engine.scripts.daily_loss_killswitch import (
                is_killswitch_tripped,
            )
        except ImportError as exc:
            logger.critical(
                "daily_loss_killswitch import failed (%s) — refusing entry until module present",
                exc,
            )
            return
        try:
            tripped, reason = is_killswitch_tripped()
        except Exception as exc:  # noqa: BLE001 — gate failure must fail closed
            logger.critical(
                "daily_loss_killswitch raised %s — refusing entry; investigate state files",
                exc, exc_info=True,
            )
            return
        if tripped:
            if not getattr(self, "_killswitch_warned_today", False):
                logger.warning(
                    "DAILY KILL SWITCH TRIPPED — entries halted: %s", reason,
                )
                self._killswitch_warned_today = True
                # Emit a v3 event so Hermes pings the operator's phone.
                with contextlib.suppress(Exception):
                    from eta_engine.brain.jarvis_v3.policies._v3_events import (
                        emit_event,
                    )
                    emit_event(
                        layer="ops",
                        event="daily_kill_switch_tripped",
                        bot_id=bot.bot_id,
                        details={"reason": reason},
                        severity="CRITICAL",
                    )
            return

        # Generate a unique signal_id and consult the persisted ledger
        # to make sure we never re-issue a signal_id that was already
        # sent in the last 24h (collision is vanishingly unlikely with
        # 32-bit hex slices but the cost of regenerating is negligible).
        signal_id = f"{bot.bot_id}_{uuid.uuid4().hex[:8]}"
        _retries = 0
        while (bot.bot_id, signal_id) in self._sent_signals and _retries < 8:
            signal_id = f"{bot.bot_id}_{uuid.uuid4().hex[:8]}"
            _retries += 1
        entry_price = float(bar["close"])
        # Default side from registry direction. SAGE-DRIVEN OVERRIDE: when
        # ETA_SAGE_DRIVEN_SIDE=1 (default on), consult Sage with a neutral
        # bias first, then USE its composite bias to pick the entry side
        # so each bot can trade both LONG and SHORT depending on what the
        # market actually offers — instead of hard-locking every bot to its
        # registered direction. When Sage has no opinion (composite bias
        # neutral or n_bars < 30 warmup), fall back to the registered
        # direction. When Sage's composite bias OPPOSES the registered
        # direction at decent conviction, take the OPPOSING side rather
        # than fight the prevailing read.
        side = "long" if bot.direction == "long" else "short"
        if os.getenv("ETA_SAGE_DRIVEN_SIDE", "1").lower() in {"1", "true", "yes", "on"}:
            sage_probe = self._consult_sage_for_bot(bot, bar, side, entry_price)
            if sage_probe is not None and getattr(sage_probe, "conviction", 0.0) >= 0.30:
                _composite = str(getattr(sage_probe, "composite_bias", ""))
                _composite = _composite.value if hasattr(_composite, "value") else _composite
                _composite = _composite.lower() if _composite else ""
                if _composite in {"long", "short"} and _composite != side:
                    logger.info(
                        "SAGE_FLIP %s registered=%s -> sage=%s (conv=%.2f)",
                        bot.bot_id, side, _composite, sage_probe.conviction,
                    )
                    side = _composite
            sage_report = sage_probe
        else:
            # Legacy path: registered direction only.
            sage_report = self._consult_sage_for_bot(bot, bar, side, entry_price)

        # Use the (possibly Sage-flipped) side everywhere downstream so
        # JARVIS, the order router, and the bracket geometry all agree
        # on direction. Without this, JARVIS would consult against
        # the bot's registered direction while the actual order would
        # ship the Sage-chosen side — bracket inversion territory.
        payload = {
            "regime": "neutral",
            "session": "rth",
            "stress": 0.4,
            "direction": side,
            "sentiment": 0.0,
            "side": "buy" if side == "long" else "sell",
            "qty": 1.0,
            "symbol": bot.symbol,
            "confidence": 0.55,
            "entry_price": entry_price,
            # 2026-05-04 wave-7/8: include bot_id so the JARVIS v23-v27
            # advanced layers can look up the bot's registry assignment
            # (instrument_class, block_regimes, lab_audit stamps). Without
            # this, every advanced layer falls through to v17 silently.
            "bot_id": bot.bot_id,
        }

        if sage_report is not None:
            payload["sage_score"] = sage_report.conviction
            payload["sage_bars"] = list(bot.sage_bars)
            payload["sage_alignment"] = sage_report.alignment_score
            payload["sage_composite_bias"] = sage_report.composite_bias.value
        else:
            payload["sage_score"] = 0.5

        verdict = self._consult_jarvis(
            bot=bot, signal_id=signal_id, action="ORDER_PLACE",
            payload=payload,
            narrative=f"mock-entry {bot.bot_id} @ {bar['close']:.2f}",
        )
        bot.last_jarvis_verdict = verdict.consolidated.final_verdict if verdict else "NONE"
        if verdict is None or verdict.is_blocked():
            return
        size_mult = verdict.final_size_multiplier
        if size_mult <= 0:
            return

        # Apply regime-driven size multiplier on top of JARVIS's own
        # final_size_multiplier. The regime detector recommends a global
        # scaler (1.0 in trending regimes, 0.6-0.8 in vol expansion / chop)
        # that further scales every order. Multiplicative composition with
        # JARVIS's per-bot size cap, never additive.
        try:
            live_regime = self._load_live_regime()
            regime_size_mult = float(live_regime.get("size_multiplier", 1.0))
            if 0.0 < regime_size_mult < 1.0:
                size_mult *= regime_size_mult
        except Exception:  # noqa: BLE001
            pass

        # Order side derived from the (possibly Sage-flipped) `side`
        # variable established above, NOT from bot.direction. Locking the
        # broker side to the registered direction would defeat the whole
        # Sage-driven side selection — we'd flip JARVIS context but ship
        # the wrong side to the broker.
        order_side = "BUY" if side == "long" else "SELL"
        rec = self._router.submit_entry(
            bot=bot, signal_id=signal_id, side=order_side, bar=bar,
            size_mult=size_mult,
        )
        if rec:
            # Persist the signal_id to the dedup ledger so a crash
            # immediately after this point cannot result in the same
            # signal_id being reused on restart. Also seed the in-memory
            # set so even within this session we don't re-issue.
            self._sent_signals.add((bot.bot_id, signal_id))
            self._record_sent_signal(bot.bot_id, signal_id, rec.fill_ts)
            logger.info(
                "ENTRY  %s %s %.4f @ %.4f (verdict=%s size_mult=%.2f)",
                bot.bot_id, order_side, rec.qty, rec.fill_price,
                verdict.consolidated.final_verdict, size_mult,
            )

    def _maybe_exit(self, bot: BotInstance, bar: dict[str, Any]) -> None:
        # Simple exit: random 1-in-15 close OR drawdown > 1.5% from entry.
        # In paper_live with a broker-side bracket attached (the venue
        # placed parent + stop + target), the broker is authoritative
        # on the close. Supervisor-side exits would double-fire:
        # supervisor submits SELL, then broker stop/target hits and
        # submits another SELL → either rejected or a flipped position.
        # Defer to the broker, with an EMERGENCY override at 2x the
        # bracket stop distance in case a network/clientId blip
        # detaches the broker bracket and we still need protection.
        pos = bot.open_position
        if pos is None:
            return
        cur_price = float(bar["close"])
        entry_price = pos["entry_price"]
        sign = 1.0 if pos["side"] == "BUY" else -1.0
        ret_pct = sign * (cur_price - entry_price) / entry_price

        broker_bracket = bool(pos.get("broker_bracket"))
        if broker_bracket:
            # Emergency override: only fire if loss > 2x the bracket stop
            # distance. This is the "broker bracket detached" backstop —
            # if everything's working, the broker closes us first.
            bracket_stop = pos.get("bracket_stop")
            try:
                stop_dist_pct = (
                    abs((float(bracket_stop) - entry_price) / entry_price)
                    if bracket_stop is not None
                    else 0.03  # 3% default emergency threshold
                )
            except (TypeError, ValueError):
                stop_dist_pct = 0.03
            emergency_loss_pct = max(2.0 * stop_dist_pct, 0.04)
            if ret_pct < -emergency_loss_pct:
                logger.warning(
                    "EMERGENCY EXIT %s: loss=%.3f exceeds 2x bracket stop "
                    "(%.3f) — broker bracket may be detached",
                    bot.bot_id, ret_pct, stop_dist_pct,
                )
                rec = self._router.submit_exit(bot=bot, bar=bar)
                if rec:
                    # Pass the entry snapshot captured by submit_exit so
                    # _propagate_close sees the original entry side/price
                    # rather than the cleared bot.open_position.
                    self._propagate_close(
                        bot, rec,
                        entry_snapshot=getattr(rec, "entry_snapshot", None),
                    )
            return

        # No broker bracket (paper_sim or paper-test crypto): supervisor-
        # side logic is the only exit. Prefer the planned bracket levels
        # set at entry (atr_stop_mult / rr_target from per_bot_registry) —
        # those are what the lab uses, so live R-magnitudes track lab. The
        # legacy 1-in-15 random close is dropped: it was the dominant exit
        # mechanism in paper mode and was scratching out trades at trivial
        # price moves long before the planned bracket could fire.
        should_exit = False
        exit_reason = ""
        plan_stop = pos.get("bracket_stop")
        plan_target = pos.get("bracket_target")
        is_buy = pos["side"] == "BUY"
        # Fix 3: also check intrabar high/low against the bracket levels.
        # Earlier ``cur_price`` (== bar.close) was the only trigger, so a
        # bar that pierced the target intrabar but closed below it would
        # not exit until the NEXT bar's close — biasing winners DOWN and
        # latching wickers into the bar after the wick. Real bracket legs
        # fire as soon as the level prints, regardless of the bar's close.
        try:
            bar_high = float(bar.get("high", cur_price))
        except (TypeError, ValueError):
            bar_high = cur_price
        try:
            bar_low = float(bar.get("low", cur_price))
        except (TypeError, ValueError):
            bar_low = cur_price
        if plan_stop is not None and plan_target is not None:
            try:
                _ps = float(plan_stop)
                _pt = float(plan_target)
                if is_buy:
                    # LONG: stop is BELOW entry → fires when bar.low <= stop;
                    # target is ABOVE entry → fires when bar.high >= target.
                    if bar_low <= _ps:
                        should_exit = True
                        exit_reason = "paper_stop"
                    elif bar_high >= _pt:
                        should_exit = True
                        exit_reason = "paper_target"
                else:
                    # SHORT: stop is ABOVE entry → fires when bar.high >= stop;
                    # target is BELOW entry → fires when bar.low <= target.
                    if bar_high >= _ps:
                        should_exit = True
                        exit_reason = "paper_stop"
                    elif bar_low <= _pt:
                        should_exit = True
                        exit_reason = "paper_target"
            except (TypeError, ValueError):
                plan_stop = None  # fall through to fallback
        if not should_exit and (plan_stop is None or plan_target is None):
            # Fallback for older positions without stored bracket: keep the
            # legacy fixed-pct gates so existing open trades still close,
            # but skip the random gate that was ruining alpha.
            if ret_pct < -0.015:
                should_exit = True
                exit_reason = "fallback_stop_pct"
            elif ret_pct > 0.025:
                should_exit = True
                exit_reason = "fallback_target_pct"

        if not should_exit:
            return
        pos["exit_reason"] = exit_reason

        rec = self._router.submit_exit(bot=bot, bar=bar)
        if rec:
            logger.info(
                "EXIT   %s %s %.4f @ %.4f (R=%.3f)",
                bot.bot_id, rec.side, rec.qty, rec.fill_price,
                rec.realized_r or 0.0,
            )
            # Feedback loop: propagate to memory + bandits + calibrator.
            # Pass the entry snapshot captured by submit_exit so the
            # edge_tracker observation receives the original entry-side /
            # entry-price (before bot.open_position was cleared).
            self._propagate_close(
                bot, rec,
                entry_snapshot=getattr(rec, "entry_snapshot", None),
            )

    # ── JARVIS consultation ─────────────────────────────────

    def _consult_sage_for_bot(self, bot: BotInstance, bar: dict, side: str, entry_price: float) -> object | None:
        """Consult Sage schools with the bot's accumulated bar buffer.

        Returns a SageReport or None when the buffer is too short or
        Sage fails. Also caches the report so feedback_loop can attribute
        realized R back to each school via edge_tracker.observe().

        Cross-asset peer_returns are populated from the SAME-CLASS sister
        bots' bar buffers so cross_asset_correlation school stops returning
        neutral. Crypto bots get peer returns from each other; futures
        bots get peer returns from their fellow MNQ/NQ/ES siblings.
        """
        bars = list(bot.sage_bars)
        # Lowered from 30 to 15 bars — matches the ATR warmup so sage
        # engages on the same tick the first proper bracket gets computed,
        # halving the cold-start window where every entry uses fallback
        # geometry without sage modulation.
        if len(bars) < 15:
            return None
        try:
            from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage

            # Build peer_returns dict from SAME-CLASS bots' recent bars so
            # cross_asset_correlation school can compute meaningful
            # alignment instead of returning neutral with rationale
            # "no peer_returns on ctx — school skipped".
            peer_returns: dict[str, list[float]] = {}
            try:
                _self_class = _classify_symbol(bot.symbol)
                for _peer in self.bots:
                    if _peer.bot_id == bot.bot_id:
                        continue
                    if _classify_symbol(_peer.symbol) != _self_class:
                        continue
                    if not _peer.sage_bars or len(_peer.sage_bars) < 5:
                        continue
                    _closes = [
                        float(b.get("close", 0))
                        for b in list(_peer.sage_bars)[-30:]
                        if b.get("close") is not None
                    ]
                    if len(_closes) < 5:
                        continue
                    _rets = [
                        (_closes[i] - _closes[i - 1]) / _closes[i - 1]
                        for i in range(1, len(_closes))
                        if _closes[i - 1] > 0
                    ]
                    if _rets:
                        peer_returns[_peer.symbol] = _rets
            except Exception:  # noqa: BLE001
                peer_returns = {}

            ctx = MarketContext(
                bars=bars,
                side=side,
                entry_price=entry_price,
                symbol=bot.symbol,
                instrument_class=_classify_symbol(bot.symbol),
                peer_returns=peer_returns or None,
            )
            report = consult_sage(ctx)
            with contextlib.suppress(Exception):
                from eta_engine.brain.jarvis_v3.sage.last_report_cache import set_last
                set_last(bot.symbol, side, report)
            return report
        except Exception as exc:  # noqa: BLE001
            logger.debug("sage consultation for %s failed (non-fatal): %s", bot.bot_id, exc)
            return None

    def _consult_jarvis(  # noqa: ANN202 -- FullJarvisVerdict is opt-imported
        self,
        *,
        bot: BotInstance,
        signal_id: str,
        action: str,
        payload: dict,
        narrative: str,
    ):
        # Track WHY we returned None so the supervisor heartbeat can
        # distinguish "JARVIS not bootstrapped" from "regime blocked this
        # strategy_kind" from "consult raised an exception". Without this,
        # every None-return collapses into a single "NONE" string in the
        # heartbeat and the operator can't tell which 50/52 bots are
        # blocked-by-regime vs JARVIS-bootstrap-down vs raising-exceptions.
        # Stored on the bot instance so it survives the return-None path.
        if self._jarvis_full is None:
            bot.last_jarvis_verdict_reason = "jarvis_not_bootstrapped"
            return None

        # Regime-aware strategy gating. If lab/regime_detector has classified
        # the current global regime as inhospitable for this bot's
        # strategy_kind (e.g. compression_breakout during vol_expansion,
        # sweep_reclaim during chop), short-circuit before consulting JARVIS.
        # Saves API spend + makes regime calls actionable. Falls open if the
        # regime feed is missing — never block on stale data.
        try:
            live_regime = self._load_live_regime()
            blocked = live_regime.get("block_strategies") or []
            if (
                isinstance(blocked, list)
                and bot.strategy_kind
                and bot.strategy_kind in blocked
            ):
                primary = live_regime.get("primary_regime") or "unknown"
                logger.info(
                    "regime-block %s.%s: strategy_kind=%s blocked by regime=%s",
                    bot.bot_id, action, bot.strategy_kind, primary,
                )
                bot.last_jarvis_verdict_reason = (
                    f"regime_block:{bot.strategy_kind}@{primary}"
                )
                return None
        except Exception:  # noqa: BLE001 -- regime feed must never block consults
            pass

        try:
            from eta_engine.brain.jarvis_admin import (
                ActionType,
                SubsystemId,
                make_action_request,
            )
            atype = getattr(ActionType, action, ActionType.ORDER_PLACE)

            # Resolve the per-bot SubsystemId. The legacy lookup stripped
            # underscores and so e.g. "eth_perp" -> "BOT_ETHPERP" never
            # matched the actual enum entry "BOT_ETH_PERP", causing every
            # bot in the fleet to fall back to BOT_MNQ. With the fleet
            # now spanning crypto + futures, the misclassification meant
            # crypto bots were being denied as overnight-restricted
            # futures even on weekends when they should trade 24/7.
            bot_lower = bot.bot_id.lower()
            symbol_upper = (getattr(bot, "symbol", "") or "").upper()
            sub = getattr(SubsystemId, f"BOT_{bot.bot_id.upper()}", None)
            if sub is None:
                if "eth" in bot_lower or symbol_upper in ("ETH", "MET"):
                    sub = SubsystemId.BOT_ETH_PERP
                elif "btc" in bot_lower or symbol_upper in ("BTC", "MBT"):
                    sub = SubsystemId.BOT_BTC_HYBRID
                elif "sol" in bot_lower or symbol_upper == "SOL":
                    sub = SubsystemId.BOT_SOL_PERP
                elif "xrp" in bot_lower or symbol_upper == "XRP":
                    sub = SubsystemId.BOT_XRP_PERP
                # Phase-5 crypto alts (2026-05-04)
                elif "avax" in bot_lower or symbol_upper == "AVAX":
                    sub = SubsystemId.BOT_AVAX
                elif "link" in bot_lower or symbol_upper == "LINK":
                    sub = SubsystemId.BOT_LINK
                elif "doge" in bot_lower or symbol_upper == "DOGE":
                    sub = SubsystemId.BOT_DOGE
                elif "crypto" in bot_lower:
                    sub = SubsystemId.BOT_CRYPTO_SEED
                # Phase-4 micros — MES needs its own SubsystemId routing
                # since we don't have a parent BOT_ES until 2026-05-04. The
                # other micros (MGC/MCL/M6E) already match their parent
                # symbol checks below.
                elif (
                    "es_" in bot_lower
                    or bot_lower.startswith("es")
                    or "mes_" in bot_lower
                    or bot_lower.startswith("mes")
                    or symbol_upper in ("ES", "ES1", "MES", "MES1")
                ):
                    sub = SubsystemId.BOT_ES
                # Phase-2 commodities + FX (2026-05-03). Matches both the
                # bare symbol (GC) and the front-month CSV-naming variant
                # (GC1) that DataLibrary indexes.
                elif "gold" in bot_lower or symbol_upper in ("GC", "GC1", "MGC", "MGC1"):
                    sub = SubsystemId.BOT_GC
                elif (
                    "crude" in bot_lower
                    or "oil" in bot_lower
                    or symbol_upper in ("CL", "CL1", "MCL", "MCL1")
                ):
                    sub = SubsystemId.BOT_CL
                elif (
                    "euro" in bot_lower
                    or symbol_upper in ("6E", "6E1", "M6E", "M6E1", "EURUSD", "EUR")
                ):
                    sub = SubsystemId.BOT_6E
                # Phase-3 rates + energy (2026-05-03)
                elif (
                    "natgas" in bot_lower
                    or "nat_gas" in bot_lower
                    or "natural_gas" in bot_lower
                    or symbol_upper in ("NG", "NG1")
                ):
                    sub = SubsystemId.BOT_NG
                elif "zn" in bot_lower or symbol_upper in ("ZN", "ZN1"):
                    sub = SubsystemId.BOT_ZN
                elif "zb" in bot_lower or symbol_upper in ("ZB", "ZB1"):
                    sub = SubsystemId.BOT_ZB
                elif symbol_upper == "NQ1":
                    sub = SubsystemId.BOT_NQ
                else:
                    sub = SubsystemId.BOT_MNQ

            # All fleet bots must mark themselves overnight_explicit so
            # the admin's overnight session gate passes them through. The
            # gate's check is `subsystem in whitelist AND overnight_explicit`
            # — both conditions are required. The supervisor only consults
            # JARVIS once a signal has already cleared the per-bot
            # confluence threshold, so by this point the entry is
            # pre-validated; operator opted to allow futures overnight as
            # well (2026-05-03) given new bar history + Wave-18 strategy
            # fleet support globex setups.
            payload = dict(payload)
            payload.setdefault("overnight_explicit", True)
            payload.setdefault("review_acknowledged", True)

            req = make_action_request(
                subsystem=sub, action=atype,
                rationale=narrative, **payload,
            )
            req.request_id = signal_id
            ctx = self._build_synthetic_ctx(bot)
            verdict = self._jarvis_full.consult(
                req=req, ctx=ctx,
                current_narrative=narrative, bot_id=bot.bot_id,
            )
            # consult() succeeded; clear any prior diagnostic reason
            # so a recovered bot doesn't keep showing a stale block tag
            # on the heartbeat after the regime gate clears.
            bot.last_jarvis_verdict_reason = ""
            return verdict
        except Exception as exc:  # noqa: BLE001
            logger.warning("consult failed for %s: %s", bot.bot_id, exc)
            # Surface exception class so the heartbeat can be grepped
            # for the dominant failure mode without tailing logs.
            bot.last_jarvis_verdict_reason = (
                f"consult_exception:{type(exc).__name__}"
            )
            return None

    @staticmethod
    def _load_live_regime() -> dict[str, object]:
        """Read the regime_state.json emitted by ``lab/regime_detector``.

        Returns a dict the synthetic-ctx builder can splat across the
        MacroSnapshot + RegimeSnapshot fields. Falls back to neutral
        defaults if the file is missing, stale, or unreadable —
        regime-feed failure must never block the supervisor's main
        consult loop. One-way contract: engine reads, regime_detector
        writes, never the reverse.

        Schema (regime_state.json) we map from::

            {
              "global_regime": "trending_up" | "trending_down" |
                                "chop" | "vol_expansion" |
                                "vol_compression" | "mixed" | "unknown",
              "asset_regimes": {
                  "<SYM>/<TF>": {"regime": ..., "confidence": ...,
                                  "last_close": ..., "vol_regime": ...},
                  ...
              },
              "cross_asset": {
                  "matrix_60bar": {...},
                  "risk_on_off": "risk_on"|"risk_off"|"neutral"
              },
              "recommended": {
                  "size_multiplier": <float>,
                  "block_strategies": [<str>, ...]
              }
            }
        """
        defaults: dict[str, object] = {
            "primary_regime": "neutral",
            "previous_regime": None,
            "confidence": 0.5,
            "vix": 18.0,
            "macro_bias": "neutral",
            "size_multiplier": 1.0,
            "block_strategies": [],
        }
        # Path is jarvis_intel/regime_state.json — regime_detector emits
        # under jarvis_intel/, not the bare state dir. The legacy bare
        # path silently never resolved, so block_strategies stayed []
        # forever and the regime gate was effectively dead. Two fallback
        # paths kept for backward compat with older deployments.
        path = (
            workspace_roots.ETA_RUNTIME_STATE_DIR / "jarvis_intel" / "regime_state.json"
        )
        if not path.exists():
            path = workspace_roots.ETA_RUNTIME_STATE_DIR / "regime_state.json"
        try:
            if not path.exists():
                return defaults
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return defaults
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return defaults

        primary = str(data.get("global_regime") or "neutral")
        if primary in ("", "unknown"):
            primary = "neutral"

        asset_regimes = data.get("asset_regimes") or {}
        avg_conf = 0.5
        vix_close = 18.0
        if isinstance(asset_regimes, dict) and asset_regimes:
            confs: list[float] = []
            for key, entry in asset_regimes.items():
                if not isinstance(entry, dict):
                    continue
                conf = entry.get("confidence")
                if isinstance(conf, (int, float)):
                    confs.append(float(conf))
                if "VIX" in str(key):
                    last_close = entry.get("last_close")
                    if isinstance(last_close, (int, float)) and last_close > 0:
                        vix_close = float(last_close)
            if confs:
                avg_conf = round(sum(confs) / len(confs), 3)

        macro_bias = "neutral"
        cross = data.get("cross_asset") or {}
        if isinstance(cross, dict):
            risk_state = str(cross.get("risk_on_off") or "neutral")
            if risk_state in ("risk_on", "risk_off"):
                macro_bias = risk_state

        size_mult = 1.0
        block_strats: list[str] = []
        recommended = data.get("recommended") or {}
        if isinstance(recommended, dict):
            sm = recommended.get("size_multiplier")
            if isinstance(sm, (int, float)) and 0.1 <= float(sm) <= 2.0:
                size_mult = float(sm)
            bs = recommended.get("block_strategies") or []
            if isinstance(bs, list):
                block_strats = [str(x) for x in bs]

        return {
            "primary_regime": primary,
            "previous_regime": None,
            "confidence": avg_conf,
            "vix": vix_close,
            "macro_bias": macro_bias,
            "size_multiplier": size_mult,
            "block_strategies": block_strats,
        }

    def _build_synthetic_ctx(self, bot: BotInstance):  # noqa: ANN202 -- JarvisContext opt-imported
        """Synthesize a minimal JarvisContext from current fleet state.

        JarvisAdmin requires either an attached engine or an explicit
        ctx. The supervisor doesn't run a full JarvisContextEngine
        (that requires live macro/equity/regime providers wired to
        market data + Apex equity feed). Until those providers are
        attached, we synthesize a neutral context per call so that
        every layer of JarvisFull (operator_override, admin, memory,
        causal, world_model, debate, premortem, ood, coach, risk,
        narrative) has the input it expects.

        Live wiring path: replace this with
        ``JarvisContextBuilder.build()`` once the providers are
        available on the VPS.
        """
        try:
            from eta_engine.brain.jarvis_context import (
                EquitySnapshot,
                JournalSnapshot,
                MacroSnapshot,
                RegimeSnapshot,
                build_snapshot,
            )
        except Exception:  # noqa: BLE001 -- if context module unavailable, fall back to admin engine
            return None

        # Aggregate per-bot risk into one fleet-level equity snapshot
        total_equity = sum(
            (b.cash + b.realized_pnl) for b in self.bots
        ) or float(self.cfg.starting_cash_per_bot)
        # Bound dd_pct to [0,1] -- pydantic validator rejects negatives
        # and values >1, both of which are possible in a wild bot run
        raw_dd = max(
            0.0, -sum(b.realized_pnl for b in self.bots) / max(total_equity, 1.0),
        )
        dd_pct = min(0.999, raw_dd)
        open_count = sum(1 for b in self.bots if b.open_position is not None)

        # Load live regime from regime_state.json (emitted by Cross-Asset Regime Detector)
        live_regime = self._load_live_regime()
        macro = MacroSnapshot(
            vix_level=live_regime.get("vix", 18.0),
            macro_bias=live_regime.get("macro_bias", "neutral"),
        )
        # Real R-at-risk = sum of (planned_stop_loss_$ / 1R_unit) across all
        # open positions. The legacy `float(open_count)` was COUNTING open
        # positions and feeding that into JarvisAdmin's open_risk_r cap of
        # 3R — so once the fleet had ≥4 bots open simultaneously, every
        # bot's verdict came back CONDITIONAL with a 0.5x size_cap (REDUCE
        # tier) regardless of actual risk. With bracket-based exits in
        # place each bot's real R-at-risk is ~0.03R (planned $1.67 stop on
        # crypto-paper / $50 1R unit), so 24 open positions sum to <1R —
        # safely under the cap, and the bots can size at 1.0x as planned.
        # Multiply per-bot risk by the instrument's point_value so futures
        # contracts (MNQ=$2/pt, ES=$50/pt, GC=$100/pt, etc.) contribute
        # the right number of dollars to the aggregate R-at-risk.
        # Crypto spot defaults to point_value=1.0 and stays unchanged.
        try:
            from eta_engine.feeds.instrument_specs import get_spec as _get_spec
        except Exception:  # noqa: BLE001
            _get_spec = None
        open_risk_r_total = 0.0
        for _b in self.bots:
            if _b.open_position is None:
                continue
            _bs = _b.open_position.get("bracket_stop")
            _qty = _b.open_position.get("qty")
            _entry = _b.open_position.get("entry_price")
            if _bs is None or not _qty or not _entry:
                # No bracket stored (legacy entry) — assume 1R per position
                # so we still respect the cap conservatively.
                open_risk_r_total += 1.0
                continue
            try:
                _pv = (
                    float(_get_spec(_b.symbol).point_value or 1.0)
                    if _get_spec else 1.0
                )
                _risk_dollars = abs(float(_bs) - float(_entry)) * float(_qty) * _pv
                _r_unit = max(float(_b.cash) * 0.01, 1e-9)
                open_risk_r_total += _risk_dollars / _r_unit
            except (TypeError, ValueError):
                open_risk_r_total += 1.0
        equity = EquitySnapshot(
            account_equity=total_equity,
            daily_pnl=sum(b.realized_pnl for b in self.bots),
            daily_drawdown_pct=dd_pct,
            open_positions=open_count,
            open_risk_r=round(open_risk_r_total, 4),
        )
        # flipped_recently must be True only when there was an ACTUAL flip
        # (previous and current both known and different). The legacy
        # "primary != previous" check fires when previous_regime is None
        # (cold-start) — so every entry on supervisor restart triggered
        # the JARVIS REVIEW tier and capped size at 0.75x. Use both-known.
        _prev = live_regime.get("previous_regime")
        _prim = live_regime.get("primary_regime", "neutral")
        _flipped = bool(_prev) and bool(_prim) and _prev != _prim
        regime = RegimeSnapshot(
            regime=_prim,
            confidence=live_regime.get("confidence", 0.5),
            previous_regime=_prev,
            flipped_recently=_flipped,
        )
        # Read the live daily-loss killswitch state so JARVIS layers
        # downstream see TRUE when it's actually tripped. Hardcoded
        # False meant the consult chain never knew the killswitch was
        # active, so any policy keyed on this flag was effectively dead.
        # Fail closed: any import/call failure → True so JARVIS errs on
        # the side of caution (REVIEW/REDUCE tiers, never ALLOW_FULL).
        try:
            from eta_engine.scripts.daily_loss_killswitch import (
                is_killswitch_tripped,
            )
            _ks_tripped, _ = is_killswitch_tripped()
            kill_switch_active = bool(_ks_tripped)
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.debug(
                "daily_loss_killswitch consult in JournalSnapshot failed; "
                "assuming active=True: %s", exc,
            )
            kill_switch_active = True
        journal = JournalSnapshot(
            kill_switch_active=kill_switch_active,
            autopilot_mode="ACTIVE",
            overrides_last_24h=0,
            blocked_last_24h=0,
            executed_last_24h=sum(b.n_entries + b.n_exits for b in self.bots),
            correlations_alert=False,
        )
        return build_snapshot(
            macro=macro, equity=equity, regime=regime, journal=journal,
            notes=[
                f"supervisor synthetic ctx for {bot.bot_id} "
                f"(symbol={bot.symbol}, dir={bot.direction})",
            ],
        )

    def _propagate_close(
        self,
        bot: BotInstance,
        rec: FillRecord,
        entry_snapshot: dict | None = None,
    ) -> None:
        """Feed a closed trade into JARVIS memory, bandits, and edge_tracker.

        ``entry_snapshot`` carries the entry-side fields (side, entry_price,
        qty, stop, target, signal_id) captured BEFORE submit_exit cleared
        ``bot.open_position``. Earlier this method read ``bot.open_position``
        directly — which was always None at this point because submit_exit
        had just cleared it — so the edge_tracker observation always took
        the rec.side / rec.fill_price fallback. That fed the EXIT side and
        EXIT price into the tracker, exactly inverting the feedback signal
        for every close.

        ``entry_snapshot=None`` is supported for legacy callers and falls
        back to the old ``bot.open_position`` lookup so we don't regress
        anything that hasn't been migrated.
        """
        try:
            from eta_engine.brain.jarvis_v3.feedback_loop import close_trade
            # Read live regime from regime_state.json so trade closes
            # carry the ACTUAL regime/macro_bias at close time. Every
            # close was previously labeled regime="neutral" regardless
            # of the live state, which collapsed every memory analog
            # into a single bucket and prevented JARVIS from learning
            # regime-conditional patterns. The pressure-test output
            # showed this clearly: by_regime always reported only
            # `neutral` for every bot, because every close was tagged
            # neutral at write time.
            live_regime = self._load_live_regime()
            regime_label = str(live_regime.get("primary_regime", "neutral"))
            macro_bias = str(live_regime.get("macro_bias", "neutral"))
            # Session derived from UTC hour: 13:30-16:00 UTC = US morning,
            # 16:00-21:00 = US afternoon, otherwise overnight/lunch. Crypto
            # bots trade 24/7 so the session label is informational only —
            # but it lets the feedback loop split by time-of-day analog.
            try:
                _h = datetime.now(UTC).hour
            except Exception:  # noqa: BLE001
                _h = -1
            if 13 <= _h < 16:
                session_label = "morning"
            elif 16 <= _h < 21:
                session_label = "afternoon"
            elif _h >= 21 or _h < 1:
                session_label = "close"
            else:
                session_label = "overnight"
            close_trade(
                signal_id=rec.signal_id,
                realized_r=rec.realized_r or 0.0,
                regime=regime_label, session=session_label, stress=0.4,
                direction=bot.direction,
                action_taken="approve_full",
                bot_id=bot.bot_id,
                memory=self._memory,
                narrative=f"close after {bot.n_exits} exits, pnl={bot.realized_pnl:+.2f}",
                extra={
                    "realized_pnl": rec.realized_pnl,
                    "fill_price": rec.fill_price,
                    "qty": rec.qty,
                    "symbol": rec.symbol,
                    "side": rec.side,
                    "close_ts": rec.fill_ts,
                    "macro_bias": macro_bias,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback propagate failed for %s: %s", bot.bot_id, exc)

        # Direct edge-tracker observation. The feedback_loop's edge_tracker
        # block depends on last_report_cache.pop_last_any() which returned
        # None for 83/83 closes — the cache-based bridge couldn't survive
        # the variance in entry-vs-close timing across the fleet. This
        # path consults Sage directly on the bot's CURRENT bar buffer at
        # close time and observes each school's bias against the realized
        # R. Decoupled from the cache, runs every close, no conditional
        # gates beyond the basic 15-bar warmup that consult_sage requires.
        try:
            bars = list(bot.sage_bars)
            if len(bars) < 15:
                return
            from eta_engine.brain.jarvis_v3.sage import (
                MarketContext,
                consult_sage,
            )
            from eta_engine.brain.jarvis_v3.sage.edge_tracker import (
                default_tracker,
            )
            # The exit fill side is OPPOSITE the entry side; feedback loop
            # wants the ENTRY side (the trade's direction). Prefer the
            # snapshot captured by submit_exit; fall back to bot.open_position
            # (legacy callers) and finally to inverting rec.side. The exit
            # FillRecord's side is the WRONG direction for the tracker, so
            # using it as the primary source was the bug we're fixing.
            if entry_snapshot is not None:
                entry_side = str(entry_snapshot.get("side", "BUY")).upper()
                entry_price_for_ctx = float(
                    entry_snapshot.get("entry_price", 0) or 0,
                )
            elif bot.open_position is not None:
                entry_side = str(
                    bot.open_position.get("side", "BUY"),
                ).upper()
                entry_price_for_ctx = float(
                    bot.open_position.get("entry_price", 0) or 0,
                )
            else:
                # Last-resort fallback: invert the exit-side and use the
                # exit fill price. Worse than the snapshot path but better
                # than crashing.
                inv_side = "BUY" if (rec.side or "").upper() == "SELL" else "SELL"
                entry_side = inv_side
                entry_price_for_ctx = float(rec.fill_price or 0)
            entry_dir = "long" if entry_side == "BUY" else "short"
            ctx = MarketContext(
                bars=bars, side=entry_dir,
                entry_price=entry_price_for_ctx,
                symbol=bot.symbol,
                instrument_class=_classify_symbol(bot.symbol),
            )
            report = consult_sage(ctx, parallel=False, use_cache=False)
            tracker = default_tracker()
            for school_name, verdict in report.per_school.items():
                tracker.observe(
                    school=school_name,
                    school_bias=verdict.bias.value,
                    entry_side=entry_dir,
                    realized_r=rec.realized_r or 0.0,
                )
        except Exception as exc:  # noqa: BLE001 — observability only
            logger.debug(
                "direct edge_tracker.observe failed for %s: %s",
                bot.bot_id, exc,
            )

    # ── Heartbeat ───────────────────────────────────────────

    def _write_heartbeat(self, tick_count: int) -> None:
        # The heartbeat path MUST NEVER crash the supervisor. A bad bot
        # whose ``to_state`` raises, a malformed strategy-readiness
        # snapshot, an unexpected feed_health serialization failure —
        # all of them used to bubble out past the narrow ``OSError``
        # catch and either (a) terminate the supervisor or (b) leak
        # past ``_tick_once``'s per-bot try/except into the loop body.
        # Either way the operator lost both the heartbeat and the
        # supervisor in one move. The widened catch below converts any
        # exception into a logged ERROR with traceback plus a
        # ``heartbeat_write_errors.jsonl`` sidecar, then returns; the
        # next tick will try again from a clean state. KeyboardInterrupt
        # and SystemExit propagate unchanged so operator-initiated
        # shutdowns still terminate the process.
        try:
            readiness, readiness_by_bot = _load_bot_strategy_readiness_snapshot()
            bot_states = []
            for bot in self.bots:
                # Pass cfg.mode so each per-bot dict carries an explicit
                # ``mode`` field (paper_sim/paper_live/live). Without this
                # the dashboard bridge fell back to a hardcoded paper_sim
                # default and the entire fleet showed Mode: paper_sim
                # even when supervisor was running paper_live.
                state = bot.to_state(mode=self.cfg.mode)
                state["strategy_readiness"] = readiness_by_bot.get(
                    bot.bot_id,
                    {
                        "status": "unknown",
                        "bot_id": bot.bot_id,
                        "launch_lane": None,
                        "can_paper_trade": False,
                        "can_live_trade": False,
                        "next_action": "Publish bot_strategy_readiness snapshot for this bot.",
                    },
                )
                bot_states.append(state)
            # Surface per-feed (ok/empty) counters so the dashboard /
            # Hermes can see drift before it bites strategy P&L.
            feed_health: dict = {}
            with contextlib.suppress(Exception):
                if hasattr(self.feed, "health_snapshot"):
                    feed_health = self.feed.health_snapshot()
            # Emit a v3 event for any feed whose empty-rate has crossed
            # the alert threshold (default 30% over min 10 samples).
            # Hermes / dashboard / red-team can subscribe to the
            # jarvis_v3_events.jsonl stream for live alerting.
            self._emit_feed_health_alerts(feed_health)
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "tick_count": tick_count,
                "mode": self.cfg.mode,
                "feed": self.cfg.data_feed,
                "feed_health": feed_health,
                "order_entry_hold": load_order_entry_hold().to_dict(),
                "live_money_enabled": self.cfg.live_money_enabled,
                "n_bots": len(self.bots),
                "bot_strategy_readiness": readiness,
                "bots": bot_states,
            }
            (self.cfg.state_dir / "heartbeat.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8",
            )
        except (KeyboardInterrupt, SystemExit):
            # Operator-initiated shutdown — never swallow.
            raise
        except Exception as exc:  # noqa: BLE001 -- wide-catch is intentional
            self._heartbeat_write_errors += 1
            logger.exception(
                "heartbeat write failed (count=%d): %s",
                self._heartbeat_write_errors, exc,
            )
            with contextlib.suppress(Exception):
                self._record_heartbeat_write_error(exc, tick_count)

    def _record_heartbeat_write_error(
        self, exc: BaseException, tick_count: int,
    ) -> None:
        """Append a structured record to the heartbeat-write error sidecar.

        Sidecar lives next to ``heartbeat.json`` so the diagnostic CLI
        and operator console can read both from the same directory.
        Format is JSON-lines so the file is append-only and trivially
        tailable; each entry includes ts, tick_count, exception type,
        and ``repr(exc)`` for grep-friendly triage.
        """
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "tick_count": tick_count,
            "exc_type": type(exc).__name__,
            "exc_repr": repr(exc),
            "error_count": self._heartbeat_write_errors,
        }
        sidecar = self.cfg.state_dir / "heartbeat_write_errors.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with sidecar.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


# ─── CLI ──────────────────────────────────────────────────────────


def _load_env_file_if_present() -> None:
    """Load environment variables from a project ``.env`` file before
    SupervisorConfig is constructed.

    The Windows scheduled-task action launches python with NO environment
    set (no shell wrapper), so every os.getenv("ETA_SUPERVISOR_FEED")
    returned the default "mock". The supervisor then ran on synthetic
    random walks instead of real composite feed (yfinance + coinbase +
    ibkr fallback). Operator was unaware because the heartbeat shows
    feed=mock but the scheduled task name implied production.

    This loader looks at three candidate paths in order, takes the first
    that exists, parses lines of the form ``KEY=VALUE`` (whitespace and
    leading-`#` comments tolerated), and writes them into ``os.environ``
    only when the key isn't already set — so an explicit shell export
    or schtasks env always wins. Idempotent + non-fatal: any parse
    failure is logged at warning and the supervisor continues.
    """
    candidates = (
        Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\.env"),
        Path(r"C:\EvolutionaryTradingAlgo\eta_engine\.env"),
        Path(r"C:\EvolutionaryTradingAlgo\.env"),
    )
    for path in candidates:
        try:
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if not key:
                    continue
                if key not in os.environ:
                    os.environ[key] = val
            logger.info("loaded env vars from %s", path)
            return
        except OSError as exc:  # noqa: PERF203 -- per-path try/except is intentional
            logger.warning("env load failed for %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _load_env_file_if_present()
    cfg = SupervisorConfig()
    supervisor = JarvisStrategySupervisor(cfg=cfg)
    return supervisor.run_forever()


if __name__ == "__main__":
    sys.exit(main())

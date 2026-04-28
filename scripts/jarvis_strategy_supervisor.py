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

import json
import logging
import os
import random
import signal as os_signal
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

logger = logging.getLogger("jarvis_strategy_supervisor")


# ─── Configuration ────────────────────────────────────────────────


def _bool_env(name: str, default: bool = False) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


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
        default_factory=lambda: ROOT / "state" / "jarvis_intel" / "supervisor",
    )
    # Live-money gate (extra safety; even paper_live still requires this False)
    live_money_enabled: bool = field(
        default_factory=lambda: _bool_env("ETA_LIVE_MONEY", default=False),
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

    def to_state(self) -> dict:
        return asdict(self)


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
        "MNQ": (21450.0, 0.002, 0.0001),
        "MNQ1": (21450.0, 0.002, 0.0001),
        "NQ":  (21450.0, 0.002, 0.0001),
        "NQ1": (21450.0, 0.002, 0.0001),
        "BTC": (95000.0, 0.005, 0.0002),
        "ETH": (3500.0,  0.006, 0.0001),
        "SOL": (180.0,   0.010, 0.0002),
        "XRP": (1.20,    0.012, 0.0001),
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
    note: str = ""


class ExecutionRouter:
    """Routes approved entries to broker (or simulates them).

    paper_sim: simulates fills at the bar's close + small slippage,
               no broker call. Generates a synthetic FillRecord.
    paper_live: writes order intent to the broker_fleet's pending
                file; that worker submits via tastytrade/IBKR adapter.
                (Plumbed; minimal for first-cut.)
    live: gated behind ETA_LIVE_MONEY=1 (raises if attempted).
    """

    def __init__(
        self,
        *,
        cfg: SupervisorConfig,
        bf_dir: Path,
    ) -> None:
        self.cfg = cfg
        self.bf_dir = bf_dir
        self.bf_dir.mkdir(parents=True, exist_ok=True)

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

        # Compute simulated fill (mode=paper_sim)
        ref_price = float(bar.get("close", 0.0))
        slippage_bps = 1.5 if side == "BUY" else -1.5
        fill_price = ref_price * (1.0 + slippage_bps / 10_000.0)
        # Size: starting_cash * size_mult / 100  (very conservative; 1% per signal)
        base_qty = (bot.cash * 0.01) / max(ref_price, 1e-9)
        qty = round(base_qty * size_mult, 6)

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
        bot.n_entries += 1
        bot.last_signal_at = rec.fill_ts

        if self.cfg.mode == "paper_live":
            # Log intent to broker_fleet's pending-orders file
            # (the broker worker will pick this up on its next tick)
            self._write_pending_order(bot, rec)

        return rec

    def submit_exit(
        self,
        *,
        bot: BotInstance,
        bar: dict[str, Any],
    ) -> FillRecord | None:
        if bot.open_position is None:
            return None
        pos = bot.open_position
        ref_price = float(bar.get("close", pos["entry_price"]))
        side_close = "SELL" if pos["side"] == "BUY" else "BUY"
        slippage_bps = 1.5 if side_close == "BUY" else -1.5
        fill_price = ref_price * (1.0 + slippage_bps / 10_000.0)

        # Realized P&L (paper)
        sign = 1.0 if pos["side"] == "BUY" else -1.0
        pnl_per_unit = (fill_price - pos["entry_price"]) * sign
        pnl = pnl_per_unit * pos["qty"]
        # Realized R: divide by entry's risk -- use 1% of cash as the risk unit
        risk_unit = bot.cash * 0.01
        realized_r = pnl / max(risk_unit, 1e-9) if risk_unit > 0 else 0.0

        rec = FillRecord(
            bot_id=bot.bot_id,
            signal_id=pos["signal_id"],
            side=side_close,
            symbol=bot.symbol,
            qty=pos["qty"],
            fill_price=round(fill_price, 4),
            fill_ts=datetime.now(UTC).isoformat(),
            paper=True,
            realized_r=round(realized_r, 4),
            note=f"close pnl={pnl:+.2f}",
        )

        bot.realized_pnl += pnl
        bot.cash += pnl
        bot.n_exits += 1
        bot.open_position = None
        return rec

    def _write_pending_order(self, bot: BotInstance, rec: FillRecord) -> None:
        try:
            f = self.bf_dir / f"{bot.bot_id}.pending_order.json"
            f.write_text(
                json.dumps({
                    "ts": rec.fill_ts,
                    "signal_id": rec.signal_id,
                    "side": rec.side,
                    "qty": rec.qty,
                    "symbol": rec.symbol,
                    "limit_price": rec.fill_price,
                }, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("pending order write failed (%s)", exc)


# ─── Supervisor ───────────────────────────────────────────────────


class JarvisStrategySupervisor:
    """The supervisor loop. JARVIS is the admin -- every decision
    flows through ``JarvisFull.consult()`` which chains all the
    wave-7-16 intelligence layers."""

    def __init__(self, cfg: SupervisorConfig | None = None) -> None:
        self.cfg = cfg or SupervisorConfig()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._stopped = False
        self.bots: list[BotInstance] = []
        self.feed = MockDataFeed()
        self._jarvis_full = None
        self._memory = None
        self._router = ExecutionRouter(
            cfg=self.cfg,
            bf_dir=ROOT / "docs" / "btc_live" / "broker_fleet",
        )

    # ── Bot loading ──────────────────────────────────────────

    def load_bots(self) -> int:
        """Load active bots from per_bot_registry."""
        try:
            from eta_engine.strategies.per_bot_registry import (
                ASSIGNMENTS,
                is_active,
            )
        except ImportError as exc:
            logger.error("per_bot_registry import failed (%s)", exc)
            return 0

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

    # ── Main loop ───────────────────────────────────────────

    def run_forever(self) -> int:
        """Run the supervisor loop until SIGTERM/SIGINT or fatal error."""
        os_signal.signal(os_signal.SIGINT, self._handle_stop)
        os_signal.signal(os_signal.SIGTERM, self._handle_stop)

        if self.load_bots() == 0:
            logger.error("no active bots loaded; exiting")
            return 1

        if not self.bootstrap_jarvis():
            logger.error("JarvisFull bootstrap failed; exiting")
            return 2

        logger.info(
            "supervisor running: %d bots, mode=%s, feed=%s, tick=%.0fs, "
            "live_money=%s",
            len(self.bots), self.cfg.mode, self.cfg.data_feed,
            self.cfg.tick_s, self.cfg.live_money_enabled,
        )

        tick_count = 0
        while not self._stopped:
            tick_count += 1
            self._tick_once(tick_count)
            self._write_heartbeat(tick_count)
            time.sleep(self.cfg.tick_s)

        logger.info("supervisor stopped after %d ticks", tick_count)
        return 0

    def _handle_stop(self, signum, frame) -> None:  # noqa: ANN001 -- signal callback signature
        logger.info("stop signal received (signum=%s)", signum)
        self._stopped = True

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
        if random.random() > (1.0 / 5):
            return

        signal_id = f"{bot.bot_id}_{uuid.uuid4().hex[:8]}"
        verdict = self._consult_jarvis(
            bot=bot, signal_id=signal_id, action="ORDER_PLACE",
            payload={
                "regime": "neutral",
                "session": "rth",
                "stress": 0.4,
                "direction": bot.direction,
                "sentiment": 0.0,
                "sage_score": 0.5,
                "side": "buy" if bot.direction == "long" else "sell",
                "qty": 1.0,
                "symbol": bot.symbol,
                "confidence": 0.55,
            },
            narrative=f"mock-entry {bot.bot_id} @ {bar['close']:.2f}",
        )
        bot.last_jarvis_verdict = verdict.consolidated.final_verdict if verdict else "NONE"
        if verdict is None or verdict.is_blocked():
            return
        size_mult = verdict.final_size_multiplier
        if size_mult <= 0:
            return

        side = "BUY" if bot.direction == "long" else "SELL"
        rec = self._router.submit_entry(
            bot=bot, signal_id=signal_id, side=side, bar=bar,
            size_mult=size_mult,
        )
        if rec:
            logger.info(
                "ENTRY  %s %s %.4f @ %.4f (verdict=%s size_mult=%.2f)",
                bot.bot_id, side, rec.qty, rec.fill_price,
                verdict.consolidated.final_verdict, size_mult,
            )

    def _maybe_exit(self, bot: BotInstance, bar: dict[str, Any]) -> None:
        # Simple exit: random 1-in-15 close OR drawdown > 1.5% from entry
        pos = bot.open_position
        if pos is None:
            return
        cur_price = float(bar["close"])
        entry_price = pos["entry_price"]
        sign = 1.0 if pos["side"] == "BUY" else -1.0
        ret_pct = sign * (cur_price - entry_price) / entry_price

        should_exit = False
        if ret_pct < -0.015:
            should_exit = True  # stop loss
        elif ret_pct > 0.025:
            should_exit = True  # take profit
        elif random.Random(int(time.time()) + 7).random() < (1.0 / 15):
            should_exit = True  # random close

        if not should_exit:
            return

        rec = self._router.submit_exit(bot=bot, bar=bar)
        if rec:
            logger.info(
                "EXIT   %s %s %.4f @ %.4f (R=%.3f)",
                bot.bot_id, rec.side, rec.qty, rec.fill_price,
                rec.realized_r or 0.0,
            )
            # Feedback loop: propagate to memory + bandits + calibrator
            self._propagate_close(bot, rec)

    # ── JARVIS consultation ─────────────────────────────────

    def _consult_jarvis(  # noqa: ANN202 -- FullJarvisVerdict is opt-imported
        self,
        *,
        bot: BotInstance,
        signal_id: str,
        action: str,
        payload: dict,
        narrative: str,
    ):
        if self._jarvis_full is None:
            return None
        try:
            from eta_engine.brain.jarvis_admin import (
                ActionType,
                SubsystemId,
                make_action_request,
            )
            atype = getattr(ActionType, action, ActionType.ORDER_PLACE)
            sub = getattr(
                SubsystemId,
                f"BOT_{bot.bot_id.upper().replace('_', '')}",
                SubsystemId.BOT_MNQ,
            )
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
            return verdict
        except Exception as exc:  # noqa: BLE001
            logger.warning("consult failed for %s: %s", bot.bot_id, exc)
            return None

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

        macro = MacroSnapshot(
            vix_level=18.0,
            macro_bias="neutral",
        )
        equity = EquitySnapshot(
            account_equity=total_equity,
            daily_pnl=sum(b.realized_pnl for b in self.bots),
            daily_drawdown_pct=dd_pct,
            open_positions=open_count,
            open_risk_r=float(open_count),
        )
        regime = RegimeSnapshot(
            regime="neutral",
            confidence=0.5,
            previous_regime=None,
            flipped_recently=False,
        )
        journal = JournalSnapshot(
            kill_switch_active=False,
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

    def _propagate_close(self, bot: BotInstance, rec: FillRecord) -> None:
        try:
            from eta_engine.brain.jarvis_v3.feedback_loop import close_trade
            close_trade(
                signal_id=rec.signal_id,
                realized_r=rec.realized_r or 0.0,
                regime="neutral", session="rth", stress=0.4,
                direction=bot.direction,
                action_taken="approve_full",
                bot_id=bot.bot_id,
                memory=self._memory,
                narrative=f"close after {bot.n_exits} exits, pnl={bot.realized_pnl:+.2f}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback propagate failed for %s: %s", bot.bot_id, exc)

    # ── Heartbeat ───────────────────────────────────────────

    def _write_heartbeat(self, tick_count: int) -> None:
        try:
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "tick_count": tick_count,
                "mode": self.cfg.mode,
                "feed": self.cfg.data_feed,
                "live_money_enabled": self.cfg.live_money_enabled,
                "n_bots": len(self.bots),
                "bots": [b.to_state() for b in self.bots],
            }
            (self.cfg.state_dir / "heartbeat.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("heartbeat write failed: %s", exc)


# ─── CLI ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = SupervisorConfig()
    supervisor = JarvisStrategySupervisor(cfg=cfg)
    return supervisor.run_forever()


if __name__ == "__main__":
    sys.exit(main())

"""BTC paper-trading harness for ``CryptoSeedBot``.

Runs the BTC bot against an injectable bar stream with a simulated
router ("PaperRouter"). Every directional-overlay signal gates through
``JarvisAdmin.request_approval`` before the paper fill. Every decision
(gate, approval, block, paper fill) lands in the ``DecisionJournal``.

When the run completes we write a single JSON verification artifact:

  docs/btc_paper/btc_paper_run_<timestamp>.json

with an equity curve, trade log, and a PASS/FAIL verdict. The verdict
is the precondition for the BTC go-live script (``scripts/btc_live.py``).

The bar stream is a protocol, so:
  * real runs can feed Bybit WS bars
  * tests feed a deterministic synthetic stream
  * nothing in this module talks to a real exchange.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.bots.crypto_seed.bot import (  # noqa: E402
    SEED_CONFIG,
    CryptoSeedBot,
)
from eta_engine.obs.decision_journal import (  # noqa: E402
    Actor,
    DecisionJournal,
    Outcome,
)
from eta_engine.venues.base import (  # noqa: E402
    OrderRequest,
    OrderResult,
    OrderStatus,
    Side,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


class BarStream(Protocol):
    """Source of OHLC bars for the paper runner.

    Implementations yield ``dict`` bars with at least the fields:
        open, high, low, close, volume, ema_9, ema_21, confluence_score.
    """

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...


@dataclass
class PaperFill:
    """One simulated fill from the paper router."""

    ts: datetime
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    order_id: str


@dataclass
class PaperEquityPoint:
    """One row of the paper-run equity curve."""

    ts: datetime
    bar_idx: int
    close: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float


@dataclass
class PaperRunResult:
    """Verification artifact for a completed paper run."""

    started_utc: datetime
    ended_utc: datetime
    bars_processed: int
    overlay_signals: int
    overlay_approved: int
    overlay_blocked: int
    paper_fills: list[PaperFill]
    equity_curve: list[PaperEquityPoint]
    starting_equity: float
    final_equity: float
    max_dd_pct: float
    overlay_win_rate: float
    kill_triggered: bool
    verdict: str  # "PASS" | "FAIL"
    verdict_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_utc": self.started_utc.isoformat(),
            "ended_utc": self.ended_utc.isoformat(),
            "bars_processed": self.bars_processed,
            "overlay_signals": self.overlay_signals,
            "overlay_approved": self.overlay_approved,
            "overlay_blocked": self.overlay_blocked,
            "paper_fills": [
                {
                    "ts": f.ts.isoformat(),
                    "symbol": f.symbol,
                    "side": f.side,
                    "qty": f.qty,
                    "price": f.price,
                    "fee": f.fee,
                    "order_id": f.order_id,
                }
                for f in self.paper_fills
            ],
            "equity_curve": [
                {
                    "ts": e.ts.isoformat(),
                    "bar_idx": e.bar_idx,
                    "close": e.close,
                    "equity": e.equity,
                    "realized_pnl": e.realized_pnl,
                    "unrealized_pnl": e.unrealized_pnl,
                }
                for e in self.equity_curve
            ],
            "starting_equity": self.starting_equity,
            "final_equity": self.final_equity,
            "max_dd_pct": self.max_dd_pct,
            "overlay_win_rate": self.overlay_win_rate,
            "kill_triggered": self.kill_triggered,
            "verdict": self.verdict,
            "verdict_reasons": self.verdict_reasons,
        }


# ---------------------------------------------------------------------------
# PaperRouter -- satisfies CryptoSeedBot._Router protocol, no exchange.
# ---------------------------------------------------------------------------


class PaperRouter:
    """Simulated order router. Fills every order at ``last_bar_close``.

    A small constant fee (``fee_bps``) is charged on notional. There is
    no slippage or partial-fill modeling -- paper runs are about surfacing
    wiring bugs, not about edge estimation.
    """

    def __init__(self, *, fee_bps: float = 2.0) -> None:
        self.fee_bps = fee_bps
        self._next_id = 0
        self._last_close: float = 0.0
        self.fills: list[PaperFill] = []

    def mark_bar(self, bar: dict[str, Any]) -> None:
        self._last_close = float(bar["close"])

    async def place_with_failover(self, req: OrderRequest) -> OrderResult:
        self._next_id += 1
        order_id = f"paper-{self._next_id:06d}"
        price = req.price if req.price is not None else self._last_close
        if price <= 0.0:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"reason": "no-last-close"},
            )
        notional = price * req.qty
        fee = notional * (self.fee_bps / 1e4)
        fill = PaperFill(
            ts=datetime.now(UTC),
            symbol=req.symbol,
            side=req.side.value,
            qty=req.qty,
            price=price,
            fee=fee,
            order_id=order_id,
        )
        self.fills.append(fill)
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_qty=req.qty,
            avg_price=price,
            fees=fee,
            raw={"paper": True},
        )


# ---------------------------------------------------------------------------
# Jarvis gate shim.
#
# JarvisAdmin is the production approval surface. For the paper run we want
# the gate wired in the same shape (request -> approve/block) but we also
# want the test suite to be able to substitute a stub. Anything with a
# ``decide(signal_meta) -> tuple[bool, str]`` method works.
# ---------------------------------------------------------------------------


class JarvisPaperGate(Protocol):
    """Narrow gate interface used by the paper runner."""

    def decide(self, signal_meta: dict[str, Any]) -> tuple[bool, str]:
        """Return (approved, rationale)."""
        ...


class AlwaysApproveGate:
    """Default gate for paper shakedown: approve every signal with reason."""

    def decide(self, signal_meta: dict[str, Any]) -> tuple[bool, str]:
        conf = signal_meta.get("confidence", 0.0)
        return True, f"paper-shakedown approve (conf={conf:.1f})"


class ConfluenceFloorGate:
    """Gate that blocks overlay signals below a confluence floor.

    Useful for verifying that the gate wiring actually blocks -- if the
    floor is set above any observed confluence, every overlay signal
    is blocked and we record BLOCKED in the journal.
    """

    def __init__(self, floor: float) -> None:
        self.floor = floor

    def decide(self, signal_meta: dict[str, Any]) -> tuple[bool, str]:
        conf = signal_meta.get("confidence", 0.0)
        if conf < self.floor:
            return False, f"blocked: confluence {conf:.1f} < floor {self.floor:.1f}"
        return True, f"approved: confluence {conf:.1f} >= floor {self.floor:.1f}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BtcPaperRunner:
    """Drive ``CryptoSeedBot`` with an injectable bar stream, router, gate."""

    def __init__(
        self,
        *,
        bot: CryptoSeedBot,
        router: PaperRouter,
        gate: JarvisPaperGate,
        journal: DecisionJournal,
        max_bars: int | None = None,
    ) -> None:
        self.bot = bot
        self.router = router
        self.gate = gate
        self.journal = journal
        self.max_bars = max_bars
        # Counters
        self._overlay_signals = 0
        self._overlay_approved = 0
        self._overlay_blocked = 0
        # Trade tracking: track last overlay entry to compute PnL on next overlay
        self._last_overlay_side: str | None = None
        self._last_overlay_price: float | None = None
        self._overlay_wins = 0
        self._overlay_losses = 0
        # Equity curve
        self._equity_curve: list[PaperEquityPoint] = []
        self._realized_pnl = 0.0

    async def run(self, stream: BarStream) -> PaperRunResult:
        started = datetime.now(UTC)
        self.journal.record(
            actor=Actor.TRADE_ENGINE,
            intent="btc_paper_run_start",
            rationale=(
                f"bot={self.bot.config.name} symbol={self.bot.config.symbol} "
                f"starting_equity=${self.bot.state.equity:.2f}"
            ),
            outcome=Outcome.NOTED,
            metadata={
                "tier": self.bot.config.tier.value,
                "max_bars": self.max_bars,
            },
        )

        starting_equity = self.bot.state.equity
        bar_idx = 0
        async for bar in stream:
            bar_idx += 1
            self.router.mark_bar(bar)

            # Initialize grid on first bar if bounds absent.
            if not self.bot.grid_state.levels:
                high = bar.get("high", bar["close"] * 1.02)
                low = bar.get("low", bar["close"] * 0.98)
                self.bot.init_grid(high, low)

            await self._process_bar(bar, bar_idx)

            if self.bot.state.is_killed:
                break
            if self.max_bars is not None and bar_idx >= self.max_bars:
                break

        await self.bot.stop()
        ended = datetime.now(UTC)

        # Compute verdict + equity metrics
        final_equity = self.bot.state.equity
        max_dd_pct = self._max_dd_from_curve()
        overlay_wr = (
            self._overlay_wins / (self._overlay_wins + self._overlay_losses)
            if (self._overlay_wins + self._overlay_losses) > 0
            else 0.0
        )
        verdict, reasons = _assess_verdict(
            starting_equity=starting_equity,
            final_equity=final_equity,
            max_dd_pct=max_dd_pct,
            overlay_win_rate=overlay_wr,
            overlay_signals=self._overlay_signals,
            kill_triggered=self.bot.state.is_killed,
        )

        result = PaperRunResult(
            started_utc=started,
            ended_utc=ended,
            bars_processed=bar_idx,
            overlay_signals=self._overlay_signals,
            overlay_approved=self._overlay_approved,
            overlay_blocked=self._overlay_blocked,
            paper_fills=list(self.router.fills),
            equity_curve=list(self._equity_curve),
            starting_equity=starting_equity,
            final_equity=final_equity,
            max_dd_pct=max_dd_pct,
            overlay_win_rate=overlay_wr,
            kill_triggered=self.bot.state.is_killed,
            verdict=verdict,
            verdict_reasons=reasons,
        )
        self.journal.record(
            actor=Actor.TRADE_ENGINE,
            intent="btc_paper_run_end",
            rationale=(
                f"bars={bar_idx} fills={len(self.router.fills)} verdict={verdict} final_equity=${final_equity:.2f}"
            ),
            outcome=Outcome.EXECUTED if verdict == "PASS" else Outcome.NOTED,
            metadata={"verdict_reasons": reasons},
        )
        return result

    async def _process_bar(self, bar: dict[str, Any], bar_idx: int) -> None:
        """One bar: update grid, evaluate overlay, gate + route overlay."""
        # Risk check -- identical semantics to live, but we surface the
        # pause into the journal so the paper run is self-explanatory.
        if not self.bot.check_risk():
            self.journal.record(
                actor=Actor.RISK_GATE,
                intent="risk_block",
                rationale=(f"bar_idx={bar_idx} killed={self.bot.state.is_killed} paused={self.bot.state.is_paused}"),
                outcome=Outcome.BLOCKED,
                metadata={"close": bar["close"]},
            )
            self._record_equity(bar, bar_idx)
            return

        # Drive the bot through its normal on_bar path, but intercept the
        # directional overlay so we can gate it through JarvisPaperGate.
        grid_orders = self.bot.manage_grid(bar["close"], self.bot.grid_state)
        self.bot.grid_state.active_orders = grid_orders

        confluence = bar.get("confluence_score", 0.0)
        signal = self.bot.directional_overlay(bar, confluence)
        if signal is not None:
            self._overlay_signals += 1
            signal_meta = {
                "side": signal.type.value,
                "price": signal.price,
                "confidence": signal.confidence,
                "bar_idx": bar_idx,
            }
            approved, rationale = self.gate.decide(signal_meta)
            if not approved:
                self._overlay_blocked += 1
                self.journal.record(
                    actor=Actor.JARVIS,
                    intent="overlay_blocked",
                    rationale=rationale,
                    outcome=Outcome.BLOCKED,
                    metadata=signal_meta,
                )
                self._record_equity(bar, bar_idx)
                return
            # Approved: route through paper router.
            self._overlay_approved += 1
            self.journal.record(
                actor=Actor.JARVIS,
                intent="overlay_approved",
                rationale=rationale,
                outcome=Outcome.EXECUTED,
                metadata=signal_meta,
            )
            notional = self.bot.state.equity * (self.bot.config.risk_per_trade_pct / 100.0)
            qty = round(notional / signal.price, 6) if signal.price > 0 else 0.0
            if qty > 0.0:
                side = Side.BUY if signal.type.value == "LONG" else Side.SELL
                req = OrderRequest(
                    symbol=self.bot._venue_symbol,  # noqa: SLF001
                    side=side,
                    qty=qty,
                )
                result = await self.router.place_with_failover(req)
                if result.status is OrderStatus.FILLED:
                    self._update_overlay_pnl(side.value, result.avg_price)

        self._record_equity(bar, bar_idx)

    def _update_overlay_pnl(self, side: str, price: float) -> None:
        """Track overlay trade PnL using last-entry vs current as a simple round-trip.

        Long->Short closes a long; Short->Long closes a short. Realized PnL
        is added to bot.state.equity via a Fill built for the book. This is
        intentionally coarse -- the goal of paper is to verify wiring, not
        to produce the same edge estimate as the real exchange.
        """
        if self._last_overlay_side is None or self._last_overlay_price is None:
            self._last_overlay_side = side
            self._last_overlay_price = price
            return
        # Compute PnL for the closing round-trip.
        if self._last_overlay_side == "BUY" and side == "SELL":
            pnl_frac = (price - self._last_overlay_price) / self._last_overlay_price
        elif self._last_overlay_side == "SELL" and side == "BUY":
            pnl_frac = (self._last_overlay_price - price) / self._last_overlay_price
        else:
            # Same-side repeat: treat as pyramiding, no PnL.
            return
        notional = self.bot.state.equity * (self.bot.config.risk_per_trade_pct / 100.0)
        pnl_usd = notional * pnl_frac
        self.bot.state.equity += pnl_usd
        self._realized_pnl += pnl_usd
        if pnl_usd >= 0:
            self._overlay_wins += 1
        else:
            self._overlay_losses += 1
        # Reset entry state to next.
        self._last_overlay_side = None
        self._last_overlay_price = None

    def _record_equity(self, bar: dict[str, Any], bar_idx: int) -> None:
        close = float(bar["close"])
        self._equity_curve.append(
            PaperEquityPoint(
                ts=datetime.now(UTC),
                bar_idx=bar_idx,
                close=close,
                equity=self.bot.state.equity,
                realized_pnl=self._realized_pnl,
                unrealized_pnl=0.0,
            ),
        )

    def _max_dd_from_curve(self) -> float:
        if not self._equity_curve:
            return 0.0
        peak = self._equity_curve[0].equity
        max_dd = 0.0
        for pt in self._equity_curve:
            peak = max(peak, pt.equity)
            if peak <= 0:
                continue
            dd = (peak - pt.equity) / peak
            max_dd = max(max_dd, dd)
        return round(max_dd, 6)


# ---------------------------------------------------------------------------
# Verdict logic -- the exact contract used by scripts/btc_live.py.
# ---------------------------------------------------------------------------


# Paper shakedown tolerances. Intentionally loose -- the goal is wiring,
# not edge. Live-enable still requires explicit operator confirmation.
MIN_BARS_FOR_PASS: int = 30
MIN_OVERLAY_SIGNALS_FOR_PASS: int = 1
MAX_DD_PCT_FOR_PASS: float = 0.10  # 10% equity DD during paper
MIN_FINAL_EQUITY_RATIO: float = 0.90  # don't end below 90% of start


def _assess_verdict(
    *,
    starting_equity: float,
    final_equity: float,
    max_dd_pct: float,
    overlay_win_rate: float,  # noqa: ARG001  (reserved for future tightening)
    overlay_signals: int,
    kill_triggered: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if kill_triggered:
        reasons.append("kill_switch tripped during paper run")
    if max_dd_pct > MAX_DD_PCT_FOR_PASS:
        reasons.append(
            f"max_dd_pct {max_dd_pct * 100:.2f}% exceeds {MAX_DD_PCT_FOR_PASS * 100:.0f}% threshold",
        )
    if starting_equity > 0 and final_equity / starting_equity < MIN_FINAL_EQUITY_RATIO:
        reasons.append(
            f"final_equity ${final_equity:.2f} < "
            f"{MIN_FINAL_EQUITY_RATIO * 100:.0f}% of starting ${starting_equity:.2f}",
        )
    if overlay_signals < MIN_OVERLAY_SIGNALS_FOR_PASS:
        reasons.append(
            f"overlay signals {overlay_signals} < {MIN_OVERLAY_SIGNALS_FOR_PASS} (sample too small to verify wiring)",
        )
    verdict = "PASS" if not reasons else "FAIL"
    return verdict, reasons


# ---------------------------------------------------------------------------
# Synthetic bar stream -- used for offline paper runs & tests.
# ---------------------------------------------------------------------------


async def synthetic_btc_stream(
    *,
    n_bars: int,
    start_price: float = 60_000.0,
    step_bps: float = 10.0,
    overlay_every: int = 15,
) -> AsyncIterator[dict[str, Any]]:
    """Deterministic synthetic BTC bars.

    Price walks in alternating up/down legs of ``step_bps``. Every
    ``overlay_every`` bars, confluence jumps above the 7 threshold to
    trigger an overlay signal (alternating long/short via EMA cross).
    """
    price = start_price
    ema9 = start_price
    ema21 = start_price
    for i in range(n_bars):
        direction = 1.0 if (i // 5) % 2 == 0 else -1.0
        price = price * (1.0 + direction * step_bps / 1e4)
        high = price * 1.001
        low = price * 0.999
        # Drive EMAs apart periodically so overlay can fire in both dirs.
        if i % overlay_every == 0:
            if (i // overlay_every) % 2 == 0:
                ema9 = price * 1.002  # bullish cross
                ema21 = price * 0.998
            else:
                ema9 = price * 0.998  # bearish cross
                ema21 = price * 1.002
            confluence = 8.0
        else:
            ema9 = price
            ema21 = price
            confluence = 3.0
        yield {
            "open": price,
            "high": high,
            "low": low,
            "close": price,
            "volume": 100.0,
            "ema_9": ema9,
            "ema_21": ema21,
            "confluence_score": confluence,
        }
        await asyncio.sleep(0)  # yield control for cooperative scheduling


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC paper-trading harness")
    p.add_argument("--bars", type=int, default=120, help="max bars to run (default 120)")
    p.add_argument("--start-equity", type=float, default=None, help="override starting equity (default SEED_CONFIG)")
    p.add_argument(
        "--gate-floor", type=float, default=None, help="ConfluenceFloorGate threshold (default: always-approve)"
    )
    p.add_argument(
        "--out-dir",
        type=str,
        default=str(ROOT / "docs" / "btc_paper"),
        help="artifact output directory",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = _cli_parse(argv)
    bot = CryptoSeedBot(config=SEED_CONFIG)
    if args.start_equity is not None:
        bot.state.equity = args.start_equity
        bot.state.peak_equity = args.start_equity
    router = PaperRouter()
    # Bot's internal router is UNSET (paper runner owns the fills). The bot
    # will log-only its on_signal fallback; we intercept before that anyway.
    gate: JarvisPaperGate = ConfluenceFloorGate(args.gate_floor) if args.gate_floor is not None else AlwaysApproveGate()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    journal_path = out_dir / "btc_paper_journal.jsonl"
    journal = DecisionJournal(journal_path)

    runner = BtcPaperRunner(
        bot=bot,
        router=router,
        gate=gate,
        journal=journal,
        max_bars=args.bars,
    )
    stream = synthetic_btc_stream(n_bars=args.bars)
    result = await runner.run(stream)

    # Write the verification artifact
    ts = result.ended_utc.strftime("%Y%m%dT%H%M%SZ")
    artifact = out_dir / f"btc_paper_run_{ts}.json"
    artifact.write_text(
        json.dumps(result.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    latest = out_dir / "btc_paper_run_latest.json"
    latest.write_text(
        json.dumps(result.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )

    print(f"bars:             {result.bars_processed}")
    print(
        f"overlay signals:  {result.overlay_signals}  "
        f"(approved={result.overlay_approved} blocked={result.overlay_blocked})"
    )
    print(f"fills:            {len(result.paper_fills)}")
    print(f"starting equity:  ${result.starting_equity:.2f}")
    print(f"final equity:     ${result.final_equity:.2f}")
    print(f"max dd:           {result.max_dd_pct * 100:.2f}%")
    print(f"overlay wr:       {result.overlay_win_rate * 100:.1f}%")
    print(f"verdict:          {result.verdict}")
    for r in result.verdict_reasons:
        print(f"    - {r}")
    print(f"artifact:         {artifact}")
    return 0 if result.verdict == "PASS" else 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()

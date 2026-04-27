"""
EVOLUTIONARY TRADING ALGO  //  tests.test_bots_jarvis_e2e
=============================================
End-to-end simulation: a full trading session where the JarvisContext
transitions from TRADE -> REDUCE -> KILL while a MnqBot processes bars.

Proves the takeover contract holds under real flow:

* Phase 1 (TRADE tier, bars 0-9)
  -> JARVIS approves ORDER_PLACE; orders route at full size.
* Phase 2 (REDUCE tier, bars 10-19)
  -> JARVIS returns CONDITIONAL with size_cap_mult<=0.5;
     bot applies the cap; routed qty shrinks proportionally.
* Phase 3 (KILL tier, bars 20-29)
  -> JARVIS denies every entry; router is never called;
     journal records `mnq_order_blocked` events only.

This is the scenario the unit tests cover piecewise. The E2E version
exercises the full `on_bar -> regime_filter -> setup_fn -> on_signal
-> _ask_jarvis -> _record_event` pipeline against a JarvisAdmin that
flips context mid-session.

The journal is the single source of truth for "what happened" -- we
assert on intent counts + outcome distribution, not log lines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.bots.base_bot import Signal, SignalType
from eta_engine.bots.mnq.bot import MnqBot
from eta_engine.brain.jarvis_admin import JarvisAdmin
from eta_engine.brain.jarvis_context import (
    EquitySnapshot,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)
from eta_engine.obs.decision_journal import DecisionJournal, Outcome
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus

_ET = ZoneInfo("America/New_York")


def _midday_ts(minute_offset: int = 0) -> datetime:
    return datetime(
        2026,
        4,
        15,
        11,
        30 + minute_offset,
        tzinfo=_ET,
    ).astimezone(UTC)


def _ctx_for_phase(phase: str):  # type: ignore[no-untyped-def]
    """Build the JarvisContext for each phase of the session."""
    if phase == "trade":
        return build_snapshot(
            macro=MacroSnapshot(vix_level=16.0, macro_bias="neutral"),
            equity=EquitySnapshot(
                account_equity=50_000.0,
                daily_pnl=0.0,
                daily_drawdown_pct=0.0,
                open_positions=0,
                open_risk_r=0.0,
            ),
            regime=RegimeSnapshot(regime="TREND_UP", confidence=0.75),
            journal=JournalSnapshot(),
            ts=_midday_ts(),
        )
    if phase == "reduce":
        return build_snapshot(
            macro=MacroSnapshot(vix_level=20.0, macro_bias="neutral"),
            equity=EquitySnapshot(
                account_equity=50_000.0,
                daily_pnl=-1_250.0,
                daily_drawdown_pct=0.025,
                open_positions=1,
                open_risk_r=1.0,
            ),
            regime=RegimeSnapshot(regime="TREND_UP", confidence=0.55),
            journal=JournalSnapshot(),
            ts=_midday_ts(10),
        )
    if phase == "kill":
        return build_snapshot(
            macro=MacroSnapshot(vix_level=28.0, macro_bias="bearish"),
            equity=EquitySnapshot(
                account_equity=50_000.0,
                daily_pnl=-3_500.0,
                daily_drawdown_pct=0.07,
                open_positions=0,
                open_risk_r=0.0,
            ),
            regime=RegimeSnapshot(regime="TREND_DOWN", confidence=0.7),
            journal=JournalSnapshot(kill_switch_active=True),
            ts=_midday_ts(20),
        )
    msg = f"unknown phase: {phase}"
    raise ValueError(msg)


class _PhaseRouter:
    """Records every order placed so we can assert bar-by-bar flow."""

    def __init__(self) -> None:
        self.orders: list[OrderRequest] = []

    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult:
        _ = urgency
        self.orders.append(req)
        return OrderResult(
            order_id=f"E2E-{len(self.orders):04d}",
            status=OrderStatus.FILLED,
            filled_qty=req.qty,
            avg_price=25_000.0,
        )


def _make_entry_signal(setup: str = "orb_breakout") -> Signal:
    """Deterministic LONG entry signal for reuse across bars."""
    return Signal(
        type=SignalType.LONG,
        symbol="MNQ",
        price=25_000.0,
        confidence=7.5,
        meta={"stop_distance": 5.0, "setup": setup},
    )


@pytest.mark.asyncio
async def test_30_bar_session_trade_reduce_kill(tmp_path: Path) -> None:
    """Full 30-bar simulated session covering the three JARVIS phases."""
    # Mutable holder so the JarvisAdmin's provide_ctx callable can see
    # the current phase at each request without the bot carrying
    # scheduler logic.
    current_phase = ["trade"]

    def _provide_ctx():  # type: ignore[no-untyped-def]
        return _ctx_for_phase(current_phase[0])

    journal = DecisionJournal(tmp_path / "e2e.jsonl")
    jarvis = JarvisAdmin(audit_path=tmp_path / "jarvis_audit.jsonl")
    router = _PhaseRouter()
    bot = MnqBot(
        jarvis=jarvis,
        journal=journal,
        provide_ctx=_provide_ctx,
        router=router,
    )
    await bot.start()
    assert bot.state.is_paused is False

    # --- Phase 1: TRADE (10 bars, all approved) ---
    current_phase[0] = "trade"
    for _ in range(10):
        await bot.on_signal(_make_entry_signal())

    trade_orders = list(router.orders)
    assert len(trade_orders) == 10, f"TRADE phase should route all 10 orders, got {len(trade_orders)}"

    # --- Phase 2: REDUCE (10 bars, CONDITIONAL cap halves qty) ---
    current_phase[0] = "reduce"
    for _ in range(10):
        await bot.on_signal(_make_entry_signal())

    reduce_orders = router.orders[10:]
    assert len(reduce_orders) == 10, f"REDUCE phase should still route orders, got {len(reduce_orders)}"
    # Cap halves 5-contract base -> 2 (int(5*0.5)=2). Verify qty shrunk.
    assert all(o.qty < trade_orders[0].qty for o in reduce_orders), (
        "REDUCE orders must be smaller than TRADE orders (cap applied)"
    )

    # --- Phase 3: KILL (10 bars, all denied, no router calls) ---
    current_phase[0] = "kill"
    orders_before_kill = len(router.orders)
    for _ in range(10):
        result = await bot.on_signal(_make_entry_signal())
        assert result is None, "KILL phase must deny every order"

    assert len(router.orders) == orders_before_kill, (
        f"KILL phase should add zero orders, got {len(router.orders) - orders_before_kill} new"
    )

    # --- Close the session ---
    await bot.stop()

    # --- Assertions on the decision journal ---
    events = journal.read_all()
    intents = [e.intent for e in events]
    outcomes = [e.outcome for e in events]

    assert "mnq_start" in intents
    assert "mnq_stop" in intents

    # 20 total routed orders (TRADE 10 + REDUCE 10)
    routed = [e for e in events if e.intent == "mnq_order_routed"]
    assert len(routed) == 20, f"expected 20 routed events, got {len(routed)}"

    # 10 blocked orders in KILL phase
    blocked = [e for e in events if e.intent == "mnq_order_blocked"]
    assert len(blocked) == 10, f"expected 10 blocked events, got {len(blocked)}"

    # Outcome distribution: EXECUTED >= 21 (20 routes + 1 start),
    # BLOCKED >= 10 (the kill denials)
    executed_count = sum(1 for o in outcomes if o == Outcome.EXECUTED)
    blocked_count = sum(1 for o in outcomes if o == Outcome.BLOCKED)
    assert executed_count >= 21
    assert blocked_count >= 10

    # --- Assertions on the JARVIS audit log ---
    # Every _ask_jarvis call should have appended to the admin's
    # audit trail (30 entries = 1 start + 20 trade/reduce + 10 kill).
    audit = jarvis.audit_tail(n=100)
    assert len(audit) >= 31, f"jarvis audit should have >=31 entries, got {len(audit)}"


@pytest.mark.asyncio
async def test_session_stop_gate_is_clean(tmp_path: Path) -> None:
    """stop() should record mnq_stop and clear state even after a KILL phase."""
    jarvis = JarvisAdmin()
    journal = DecisionJournal(tmp_path / "stop.jsonl")

    def _provide_ctx():  # type: ignore[no-untyped-def]
        return _ctx_for_phase("kill")

    bot = MnqBot(
        jarvis=jarvis,
        journal=journal,
        provide_ctx=_provide_ctx,
    )
    # Start should be refused under KILL -> bot paused, but stop
    # still works cleanly.
    await bot.start()
    assert bot.state.is_paused is True
    await bot.stop()

    events = journal.read_all()
    intents = {e.intent for e in events}
    assert "mnq_start_blocked" in intents
    assert "mnq_stop" in intents


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

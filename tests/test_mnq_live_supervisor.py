"""
Tests for :mod:`scripts.mnq_live_supervisor`.

Exercises the bar-consumption loop end-to-end with a stubbed bar source
+ fake router. Proves:

  * supervisor.start delegates to MnqBot.start (JARVIS STRATEGY_DEPLOY gate
    still fires), persists supervisor state;
  * each consumed bar runs through MnqBot.on_bar -> on_signal -> router,
    and the supervisor counters reflect journal EXECUTED/BLOCKED events;
  * source exhaustion yields an empty snapshot without raising;
  * on_bar exceptions are quarantined into supervisor.state.last_event
    instead of crashing the loop;
  * JsonlBarSource reads a JSONL file line-by-line.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from eta_engine.bots.mnq.bot import MnqBot
from eta_engine.brain.jarvis_admin import JarvisAdmin
from eta_engine.brain.jarvis_context import (
    EquitySnapshot,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)
from eta_engine.obs.decision_journal import DecisionJournal
from eta_engine.scripts.mnq_live_supervisor import (
    JsonlBarSource,
    MnqLiveSupervisor,
)
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus

if TYPE_CHECKING:
    from pathlib import Path


_ET = ZoneInfo("America/New_York")


def _trade_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.7),
        journal=JournalSnapshot(),
        ts=datetime(2026, 4, 15, 12, 0, tzinfo=_ET).astimezone(UTC),
    )


def _kill_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-3_000.0,
            daily_drawdown_pct=0.06,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_DOWN", confidence=0.7),
        journal=JournalSnapshot(kill_switch_active=True),
        ts=datetime(2026, 4, 15, 12, 0, tzinfo=_ET).astimezone(UTC),
    )


class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list[OrderRequest] = []

    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult:
        _ = urgency
        self.calls.append(req)
        return OrderResult(
            order_id=f"F-{len(self.calls):04d}",
            status=OrderStatus.FILLED,
            filled_qty=req.qty,
            avg_price=25_000.0,
        )


class _StaticBarSource:
    """Async iterator returning pre-canned bars."""

    def __init__(self, bars: list[dict[str, Any]]) -> None:
        self._bars = list(bars)

    def __aiter__(self) -> Any:  # noqa: ANN401 -- async generator type is awkward to annotate
        return self._gen()

    async def _gen(self) -> Any:  # noqa: ANN401 -- async generator type is awkward to annotate
        for bar in self._bars:
            yield bar


def _make_bot(tmp_path: Path, ctx_fn, *, router: _FakeRouter) -> tuple[MnqBot, DecisionJournal, JarvisAdmin]:  # type: ignore[no-untyped-def]
    journal = DecisionJournal(tmp_path / "mnq.jsonl")
    jarvis = JarvisAdmin(audit_path=tmp_path / "mnq_audit.jsonl")
    bot = MnqBot(
        jarvis=jarvis,
        provide_ctx=ctx_fn,
        router=router,
        journal=journal,
    )
    return bot, journal, jarvis


def _orb_bar() -> dict[str, Any]:
    """Bar that fires the ORB breakout setup in MnqBot.

    ATR kept small so stop_distance * POINT_VALUE_USD leaves room for
    enough contracts to survive JARVIS's CONDITIONAL size cap and
    still round to >=1 after int() truncation.
    """
    return {
        "ts": "2026-04-15T15:00:00+00:00",
        "open": 25_000,
        "high": 25_050,
        "low": 24_990,
        "close": 25_050,
        "volume": 5000,
        "avg_volume": 1000,
        "orb_high": 25_040,
        "orb_low": 24_900,
        "atr_14": 1.0,  # -> stop_distance 1.5 pts -> ~16 contracts base
        "adx_14": 35.0,
    }


# --------------------------------------------------------------------------- #
# start() gates through JARVIS + persists state
# --------------------------------------------------------------------------- #
class TestSupervisorStart:
    def test_start_under_trade_persists_state(self, tmp_path: Path) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _trade_ctx, router=router)
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        asyncio.run(sup.start())
        assert sup.state.paused is False
        state_file = tmp_path / "out" / "mnq_live_state.json"
        assert state_file.exists()
        snap = json.loads(state_file.read_text(encoding="utf-8"))
        assert snap["symbol"] == "MNQ"
        assert snap["router_name"] == "_FakeRouter"
        assert snap["paused"] is False

    def test_start_under_kill_persists_paused(self, tmp_path: Path) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _kill_ctx, router=router)
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        asyncio.run(sup.start())
        assert sup.state.paused is True


# --------------------------------------------------------------------------- #
# run_one_bar + run_n_bars -- counter tracking
# --------------------------------------------------------------------------- #
class TestBarConsumption:
    def test_run_one_bar_routes_order_and_increments_counters(
        self,
        tmp_path: Path,
    ) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _trade_ctx, router=router)
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([_orb_bar()]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        asyncio.run(sup.start())
        snap = asyncio.run(sup.run_one_bar())
        assert snap["bars_consumed"] == 1
        assert snap["signals_routed"] == 1, f"expected 1 routed order, got {snap['signals_routed']}"
        assert len(router.calls) == 1

    def test_run_one_bar_blocked_under_kill(self, tmp_path: Path) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _kill_ctx, router=router)
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([_orb_bar()]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        # Start under kill -> bot paused, but we still push a bar
        # through to prove the gate rejects it cleanly.
        asyncio.run(sup.start())
        snap = asyncio.run(sup.run_one_bar())
        # Paused bot's check_risk short-circuits on_bar before signals
        # fire, so routed + blocked both stay 0 in this path. What we
        # assert is that the supervisor consumed the bar without crashing.
        assert snap["bars_consumed"] == 1
        assert snap["paused"] is True
        assert len(router.calls) == 0

    def test_exhausted_source_returns_empty_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _trade_ctx, router=router)
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        asyncio.run(sup.start())
        snap = asyncio.run(sup.run_one_bar())
        assert snap == {}
        assert sup.state.last_event == "bar_source_exhausted"

    def test_run_n_bars_stops_at_source_end(self, tmp_path: Path) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _trade_ctx, router=router)
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([_orb_bar(), _orb_bar()]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        asyncio.run(sup.start())
        snaps = asyncio.run(sup.run_n_bars(10))
        assert len(snaps) == 2
        assert snaps[-1]["bars_consumed"] == 2


# --------------------------------------------------------------------------- #
# on_bar error quarantine
# --------------------------------------------------------------------------- #
class TestErrorQuarantine:
    def test_on_bar_exception_does_not_crash_supervisor(
        self,
        tmp_path: Path,
    ) -> None:
        router = _FakeRouter()
        bot, journal, jarvis = _make_bot(tmp_path, _trade_ctx, router=router)

        async def _broken_on_bar(_bar):  # type: ignore[no-untyped-def]
            msg = "forced breakage"
            raise RuntimeError(msg)

        bot.on_bar = _broken_on_bar  # type: ignore[method-assign]
        sup = MnqLiveSupervisor(
            bot=bot,
            bar_source=_StaticBarSource([_orb_bar()]),
            out_dir=tmp_path / "out",
            journal=journal,
            jarvis=jarvis,
        )
        asyncio.run(sup.start())
        snap = asyncio.run(sup.run_one_bar())
        assert snap["bars_consumed"] == 1
        assert "on_bar_error:RuntimeError" in snap["last_event"]


# --------------------------------------------------------------------------- #
# JsonlBarSource
# --------------------------------------------------------------------------- #
class TestJsonlBarSource:
    def test_yields_bars_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "bars.jsonl"
        path.write_text(
            "\n".join(json.dumps({"i": i, "close": 25_000 + i}) for i in range(3)) + "\n",
            encoding="utf-8",
        )
        src = JsonlBarSource(path)

        async def _collect() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            async for bar in src:
                out.append(bar)
            return out

        bars = asyncio.run(_collect())
        assert len(bars) == 3
        assert bars[0]["i"] == 0
        assert bars[2]["close"] == 25_002

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        src = JsonlBarSource(tmp_path / "nope.jsonl")

        async def _collect() -> list:
            out = []
            async for bar in src:
                out.append(bar)
            return out

        assert asyncio.run(_collect()) == []

    def test_invalid_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "bars.jsonl"
        path.write_text(
            '{broken\n{"ok": 1}\n\n{"ok": 2}\n',
            encoding="utf-8",
        )
        src = JsonlBarSource(path)

        async def _collect() -> list:
            return [b async for b in src]

        bars = asyncio.run(_collect())
        assert bars == [{"ok": 1}, {"ok": 2}]

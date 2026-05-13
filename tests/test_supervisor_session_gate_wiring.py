"""Tests for the JARVIS strategy supervisor's SessionGate + daily-loss wiring.

Covers the three runtime guarantees the wiring is supposed to deliver:

1. Bots with ``enable_session_gate=True`` in registry ``extras["edge_config"]``
   do NOT submit_entry calls when the gate reports the bar is outside
   RTH (or inside the EoD cutoff window, or during a news blackout).
2. The supervisor force-flattens an open position when the gate's
   ``should_flatten_eod`` fires (15:59 CT for futures bots).
3. Per-bot ``daily_loss_limit_pct`` from the registry blocks new
   entries once cumulative realized PnL since session start crosses
   the configured floor — and clears at the next session rollover.

Plus targeted tests for the wiring helpers themselves so a refactor
that breaks the policy layer is caught at the seam, not three layers
deep inside the supervisor.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest

from eta_engine.core.events_calendar import CalendarEvent, EventsCalendar
from eta_engine.scripts.jarvis_strategy_supervisor import (
    BotInstance,
    JarvisStrategySupervisor,
    SupervisorConfig,
)
from eta_engine.scripts.supervisor_session_wiring import (
    BotSessionState,
    build_session_gate,
    current_session_date,
    enforce_daily_loss_cap,
    evaluate_pre_entry_gate,
    extract_daily_loss_limit_pct,
    extract_gate_flags,
    should_flatten_now,
    update_daily_loss_anchor,
)

# ----------------------------------------------------------------------- #
# Helpers
# ----------------------------------------------------------------------- #


def _ct_to_utc(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    """Return ``hh:mm`` Chicago time as a UTC datetime (CDT, March-Nov)."""
    return datetime(y, m, d, hh, mm, tzinfo=UTC) + timedelta(hours=5)


def _futures_extras_with_gate() -> dict[str, Any]:
    return {
        "edge_config": {
            "enable_session_gate": True,
            "is_crypto": False,
            "strategy_mode": "trend",
        },
        "daily_loss_limit_pct": 3.0,
    }


def _crypto_extras_with_gate() -> dict[str, Any]:
    return {
        "edge_config": {
            "enable_session_gate": True,
            "is_crypto": True,
            "strategy_mode": "trend",
        },
        "daily_loss_limit_pct": 4.0,
    }


def _extras_no_gate() -> dict[str, Any]:
    return {
        "edge_config": {
            "enable_session_gate": False,
            "is_crypto": False,
        },
        "daily_loss_limit_pct": 2.0,
    }


# ----------------------------------------------------------------------- #
# Helper module: extract_gate_flags / extract_daily_loss_limit_pct
# ----------------------------------------------------------------------- #


class TestExtractFlags:
    def test_extract_gate_flags_finds_edge_config(self) -> None:
        flags = extract_gate_flags(_futures_extras_with_gate())
        assert flags["enable_session_gate"] is True
        assert flags["is_crypto"] is False
        assert flags["strategy_mode"] == "trend"

    def test_extract_gate_flags_handles_none(self) -> None:
        assert extract_gate_flags(None) == {}

    def test_extract_gate_flags_handles_missing_edge_config(self) -> None:
        assert extract_gate_flags({"daily_loss_limit_pct": 3.0}) == {}

    def test_extract_daily_loss_limit_pct_picks_up_value(self) -> None:
        assert extract_daily_loss_limit_pct({"daily_loss_limit_pct": 4.0}) == 4.0

    def test_extract_daily_loss_limit_pct_falls_back_on_invalid(self) -> None:
        assert extract_daily_loss_limit_pct({"daily_loss_limit_pct": "x"}) == 2.5
        assert extract_daily_loss_limit_pct({"daily_loss_limit_pct": -1.0}) == 2.5
        assert extract_daily_loss_limit_pct(None) == 2.5


# ----------------------------------------------------------------------- #
# build_session_gate: constructed shapes for futures vs crypto vs disabled
# ----------------------------------------------------------------------- #


class TestBuildSessionGate:
    def test_disabled_returns_none(self) -> None:
        assert build_session_gate(symbol="MNQ", extras=_extras_no_gate()) is None
        assert build_session_gate(symbol="MNQ", extras=None) is None
        assert build_session_gate(symbol="MNQ", extras={}) is None

    def test_futures_gate_uses_chicago_rth(self) -> None:
        gate = build_session_gate(symbol="MNQ", extras=_futures_extras_with_gate())
        assert gate is not None
        assert gate.config.timezone_name == "America/Chicago"
        assert gate.config.rth_start_local == time(8, 30)
        assert gate.config.rth_end_local == time(16, 0)
        assert gate.config.eod_cutoff_local == time(15, 59)

    def test_crypto_gate_uses_24_7_window(self) -> None:
        gate = build_session_gate(symbol="BTC", extras=_crypto_extras_with_gate())
        assert gate is not None
        assert gate.config.timezone_name == "UTC"
        assert gate.config.rth_start_local == time(0, 0)
        # 24/7 window: cutoff sentinel at 23:59:59 — never fires in practice
        assert gate.config.eod_cutoff_local == time(23, 59, 59)


# ----------------------------------------------------------------------- #
# evaluate_pre_entry_gate / should_flatten_now: state=None and gate=None paths
# ----------------------------------------------------------------------- #


class TestPolicyPathsLegacyBypass:
    def test_no_state_allows_entry(self) -> None:
        ok, reason = evaluate_pre_entry_gate(None, now=_ct_to_utc(2026, 5, 15, 4, 0))
        assert ok is True
        assert reason == ""

    def test_no_state_no_flatten(self) -> None:
        flat, reason = should_flatten_now(None, now=_ct_to_utc(2026, 5, 15, 23, 0))
        assert flat is False
        assert reason == ""

    def test_state_with_no_gate_allows_entry(self) -> None:
        state = BotSessionState(gate=None)
        ok, _ = evaluate_pre_entry_gate(state, now=_ct_to_utc(2026, 5, 15, 4, 0))
        assert ok is True


# ----------------------------------------------------------------------- #
# evaluate_pre_entry_gate: futures-bot RTH semantics
# ----------------------------------------------------------------------- #


class TestPreEntryGateFutures:
    def test_blocks_outside_rth(self) -> None:
        gate = build_session_gate(symbol="MNQ", extras=_futures_extras_with_gate())
        state = BotSessionState(gate=gate)
        ok, reason = evaluate_pre_entry_gate(state, now=_ct_to_utc(2026, 5, 15, 4, 0))
        assert ok is False
        assert reason == "outside_rth"

    def test_allows_during_rth(self) -> None:
        gate = build_session_gate(symbol="MNQ", extras=_futures_extras_with_gate())
        state = BotSessionState(gate=gate)
        ok, reason = evaluate_pre_entry_gate(
            state,
            now=_ct_to_utc(2026, 5, 15, 10, 0),
        )
        assert ok is True
        assert reason == ""

    def test_blocks_at_eod_cutoff(self) -> None:
        gate = build_session_gate(symbol="MNQ", extras=_futures_extras_with_gate())
        state = BotSessionState(gate=gate)
        ok, reason = evaluate_pre_entry_gate(
            state,
            now=_ct_to_utc(2026, 5, 15, 15, 59),
        )
        assert ok is False
        assert reason == "eod_cutoff"

    def test_news_blackout_blocks_entries(self) -> None:
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="FOMC",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 13, 0),
                    impact="high",
                ),
            ]
        )
        gate = build_session_gate(
            symbol="MNQ",
            extras=_futures_extras_with_gate(),
            calendar=cal,
        )
        state = BotSessionState(gate=gate)
        ok, reason = evaluate_pre_entry_gate(
            state,
            now=_ct_to_utc(2026, 5, 15, 12, 50),
        )
        assert ok is False
        assert "news_blackout" in reason
        assert "FOMC" in reason


# ----------------------------------------------------------------------- #
# should_flatten_now: futures bot at 15:59 CT
# ----------------------------------------------------------------------- #


class TestShouldFlattenNow:
    def test_flatten_fires_at_eod_cutoff(self) -> None:
        gate = build_session_gate(symbol="MNQ", extras=_futures_extras_with_gate())
        state = BotSessionState(gate=gate)
        flat, reason = should_flatten_now(state, now=_ct_to_utc(2026, 5, 15, 15, 59))
        assert flat is True
        assert reason == "eod_pending"

    def test_no_flatten_mid_day(self) -> None:
        gate = build_session_gate(symbol="MNQ", extras=_futures_extras_with_gate())
        state = BotSessionState(gate=gate)
        flat, _ = should_flatten_now(state, now=_ct_to_utc(2026, 5, 15, 11, 0))
        assert flat is False

    def test_crypto_gate_does_not_force_flatten(self) -> None:
        """Crypto bots use a 23:59:59 cutoff — should_flatten_eod must not fire
        at any normal trading hour."""
        gate = build_session_gate(symbol="BTC", extras=_crypto_extras_with_gate())
        state = BotSessionState(gate=gate)
        # Pick a few representative UTC times across the 24-hour cycle
        for hh in (1, 8, 13, 19, 23):
            flat, _ = should_flatten_now(
                state,
                now=datetime(2026, 5, 15, hh, 0, tzinfo=UTC),
            )
            assert flat is False, f"crypto flatten fired at hour={hh}"


# ----------------------------------------------------------------------- #
# Daily-loss cap: anchor rollover + halt + clear at next session
# ----------------------------------------------------------------------- #


class TestDailyLossCap:
    def test_anchor_initializes_on_first_call(self) -> None:
        state = BotSessionState()
        rolled = update_daily_loss_anchor(
            state,
            realized_pnl=100.0,
            now=datetime(2026, 5, 15, 14, 0, tzinfo=UTC),
        )
        assert rolled is True
        assert state.daily_pnl_anchor == pytest.approx(100.0)
        assert state.daily_session_date != ""

    def test_anchor_does_not_roll_within_session(self) -> None:
        state = BotSessionState()
        update_daily_loss_anchor(
            state,
            realized_pnl=100.0,
            now=datetime(2026, 5, 15, 14, 0, tzinfo=UTC),
        )
        rolled = update_daily_loss_anchor(
            state,
            realized_pnl=200.0,
            now=datetime(2026, 5, 15, 18, 0, tzinfo=UTC),
        )
        assert rolled is False
        assert state.daily_pnl_anchor == pytest.approx(100.0)

    def test_cap_halts_when_loss_exceeds_limit(self) -> None:
        state = BotSessionState()
        now = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
        # Pre-warm the anchor at 0 (session start, no PnL yet).
        update_daily_loss_anchor(state, realized_pnl=0.0, now=now)
        # Now -$200 cumulative against $5,000 starting cash = -4%
        # which is > 3% limit → halted.
        halted, loss_pct = enforce_daily_loss_cap(
            state,
            realized_pnl=-200.0,
            starting_cash=5_000.0,
            daily_loss_limit_pct=3.0,
            now=now,
        )
        assert halted is True
        assert loss_pct == pytest.approx(4.0)
        assert state.halted_until_session_date != ""

    def test_cap_does_not_halt_within_limit(self) -> None:
        state = BotSessionState()
        now = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
        update_daily_loss_anchor(state, realized_pnl=0.0, now=now)
        # Loss = $100 / $5,000 = 2% < 3% limit → NOT halted.
        halted, loss_pct = enforce_daily_loss_cap(
            state,
            realized_pnl=-100.0,
            starting_cash=5_000.0,
            daily_loss_limit_pct=3.0,
            now=now,
        )
        assert halted is False
        assert loss_pct == pytest.approx(2.0)

    def test_halt_clears_at_next_session(self) -> None:
        state = BotSessionState()
        # Day 1: anchor at 0, lose $200 → trip
        now1 = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
        update_daily_loss_anchor(state, realized_pnl=0.0, now=now1)
        enforce_daily_loss_cap(
            state,
            realized_pnl=-200.0,
            starting_cash=5_000.0,
            daily_loss_limit_pct=3.0,
            now=now1,
        )
        assert state.halted_until_session_date != ""
        # Day 2: anchor rolls to current realized_pnl (-200), session_pnl=0
        now2 = datetime(2026, 5, 17, 14, 0, tzinfo=UTC)  # +2 days for safety
        halted, _ = enforce_daily_loss_cap(
            state,
            realized_pnl=-200.0,
            starting_cash=5_000.0,
            daily_loss_limit_pct=3.0,
            now=now2,
        )
        # Anchor rolled to -200 → session_pnl = 0 → not halted
        assert halted is False
        assert state.halted_until_session_date == ""

    def test_session_pnl_uses_anchor_not_cumulative(self) -> None:
        """A bot that started the day at +500 cumulative and is now at +400
        should NOT be halted: session PnL is -100 (not -400)."""
        state = BotSessionState()
        # Day 1 start: anchor at +500
        now1 = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
        update_daily_loss_anchor(state, realized_pnl=500.0, now=now1)
        halted, loss_pct = enforce_daily_loss_cap(
            state,
            realized_pnl=400.0,
            starting_cash=5_000.0,
            daily_loss_limit_pct=3.0,
            now=now1,
        )
        assert halted is False
        assert loss_pct == pytest.approx(2.0)


def test_current_session_date_is_iso_in_eastern_tz() -> None:
    # 2026-05-15 04:00 UTC is 00:00 ET (EDT, UTC-4) — same day
    s = current_session_date(datetime(2026, 5, 15, 4, 0, tzinfo=UTC))
    assert s == "2026-05-15"
    # 2026-05-15 03:00 UTC is 23:00 ET on 2026-05-14
    s = current_session_date(datetime(2026, 5, 15, 3, 0, tzinfo=UTC))
    assert s == "2026-05-14"


# ----------------------------------------------------------------------- #
# Supervisor-level integration: _maybe_enter blocks outside RTH
# ----------------------------------------------------------------------- #


def _make_supervisor_with_gated_bot(
    *,
    symbol: str = "MNQ",
    extras: dict[str, Any] | None = None,
    daily_cap: float = 3.0,
) -> tuple[JarvisStrategySupervisor, BotInstance]:
    """Build a supervisor with a single bot whose session_state is wired."""
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.data_feed = "mock"
    sup = JarvisStrategySupervisor(cfg=cfg)
    gate = build_session_gate(
        symbol=symbol,
        extras=extras or _futures_extras_with_gate(),
    )
    bot = BotInstance(
        bot_id="test_bot",
        symbol=symbol,
        strategy_kind="x",
        direction="long",
        cash=5_000.0,
        daily_loss_limit_pct=daily_cap,
        session_state=BotSessionState(gate=gate),
    )
    sup.bots.append(bot)
    return sup, bot


def test_maybe_enter_skips_submit_entry_outside_rth(monkeypatch) -> None:
    """A bot with enable_session_gate=True must not call submit_entry when
    the gate's RTH window is closed (overnight / pre-market)."""
    sup, bot = _make_supervisor_with_gated_bot()
    monkeypatch.setattr("random.random", lambda: 0.0)  # always fire dice
    submit_entry_called: list[bool] = []
    monkeypatch.setattr(
        sup._router,
        "submit_entry",
        lambda **kwargs: submit_entry_called.append(True) or None,
    )
    # Pin "now" inside _maybe_enter to outside-RTH (04:00 CT)
    fake_now = _ct_to_utc(2026, 5, 15, 4, 0)
    monkeypatch.setattr(
        "eta_engine.scripts.jarvis_strategy_supervisor.datetime",
        _FakeDatetime(fake_now),
    )

    bar = {"close": 21450.0, "high": 21455.0, "low": 21445.0, "open": 21450.0}
    sup._maybe_enter(bot, bar)

    assert submit_entry_called == []


def test_maybe_enter_allows_submit_entry_during_rth(monkeypatch) -> None:
    """Same gated bot — entry is allowed at 10:00 CT. We patch the JARVIS
    consult so the test doesn't need the full intelligence stack online."""
    sup, bot = _make_supervisor_with_gated_bot()
    monkeypatch.setattr("random.random", lambda: 0.0)
    monkeypatch.setattr(
        "eta_engine.feeds.capital_allocator.resolve_execution_target",
        lambda bot_id, prospective_loss_usd: ("live", "test_live"),
    )

    submit_called: list[dict[str, Any]] = []

    def _fake_submit_entry(**kwargs: Any) -> None:
        submit_called.append(kwargs)
        return None

    monkeypatch.setattr(sup._router, "submit_entry", _fake_submit_entry)

    # Stub JARVIS consult to a permissive verdict so we reach submit_entry.
    class _FakeConsolidated:
        final_verdict = "APPROVED"

    class _FakeVerdict:
        consolidated = _FakeConsolidated()
        final_size_multiplier = 1.0

        def is_blocked(self) -> bool:
            return False

    monkeypatch.setattr(
        sup,
        "_consult_jarvis",
        lambda **kwargs: _FakeVerdict(),
    )
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *a, **kw: None)
    monkeypatch.setattr(
        sup,
        "_check_signal_aggregation",
        lambda **kwargs: "",
    )
    # Stub the kill-switch (it imports a module that reads files).
    monkeypatch.setattr(
        "eta_engine.scripts.daily_loss_killswitch.is_killswitch_tripped",
        lambda: (False, ""),
        raising=False,
    )

    fake_now = _ct_to_utc(2026, 5, 15, 10, 0)  # mid-RTH
    monkeypatch.setattr(
        "eta_engine.scripts.jarvis_strategy_supervisor.datetime",
        _FakeDatetime(fake_now),
    )

    bar = {"close": 21450.0, "high": 21455.0, "low": 21445.0, "open": 21450.0}
    sup._maybe_enter(bot, bar)

    # submit_entry was reached (even if the request itself was a no-op return).
    # In paper_sim with the patched router stub, the call is recorded.
    assert len(submit_called) == 1


def test_maybe_enter_routes_eval_paper_into_local_paper_sim(monkeypatch) -> None:
    """EVAL_PAPER means no broker submit, but paper_sim must still simulate.

    Regression coverage for the paper-soak lane: the lifecycle gate returns
    target="paper" for eval bots. In paper_sim mode that should continue into
    the local simulator so the bot can build a soak ledger instead of only
    writing shadow signals.
    """
    sup, bot = _make_supervisor_with_gated_bot()
    monkeypatch.setattr("random.random", lambda: 0.0)
    monkeypatch.setattr(
        "eta_engine.feeds.capital_allocator.resolve_execution_target",
        lambda bot_id, prospective_loss_usd: ("paper", "lifecycle_eval_paper"),
    )

    submit_called: list[dict[str, Any]] = []

    def _fake_submit_entry(**kwargs: Any) -> None:
        submit_called.append(kwargs)
        return None

    monkeypatch.setattr(sup._router, "submit_entry", _fake_submit_entry)

    class _FakeConsolidated:
        final_verdict = "APPROVED"

    class _FakeVerdict:
        consolidated = _FakeConsolidated()
        final_size_multiplier = 1.0

        def is_blocked(self) -> bool:
            return False

    monkeypatch.setattr(sup, "_consult_jarvis", lambda **kwargs: _FakeVerdict())
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *a, **kw: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **kwargs: "")
    monkeypatch.setattr(
        "eta_engine.scripts.daily_loss_killswitch.is_killswitch_tripped",
        lambda: (False, ""),
        raising=False,
    )
    monkeypatch.setattr(
        "eta_engine.scripts.shadow_signal_logger.log_shadow_signal",
        lambda **kwargs: None,
    )

    fake_now = _ct_to_utc(2026, 5, 15, 10, 0)
    monkeypatch.setattr(
        "eta_engine.scripts.jarvis_strategy_supervisor.datetime",
        _FakeDatetime(fake_now),
    )

    bar = {"close": 21450.0, "high": 21455.0, "low": 21445.0, "open": 21450.0}
    sup._maybe_enter(bot, bar)

    assert len(submit_called) == 1


def test_maybe_flatten_for_eod_calls_submit_exit(monkeypatch) -> None:
    """When the gate signals EoD-pending and the bot has an open position,
    the supervisor force-flattens via _router.submit_exit and propagates
    the close through the feedback path."""
    sup, bot = _make_supervisor_with_gated_bot()
    bot.open_position = {
        "side": "BUY",
        "entry_price": 21450.0,
        "qty": 1,
        "bracket_stop": 21430.0,
        "bracket_target": 21490.0,
        "signal_id": "sig-x",
        "opened_at": "2026-05-15T14:00:00Z",
    }

    submit_exit_called: list[dict[str, Any]] = []
    propagated: list[Any] = []

    class _FakeFill:
        signal_id = "sig-x"
        realized_r = -0.5
        realized_pnl = -50.0
        side = "SELL"
        symbol = "MNQ"
        qty = 1.0
        fill_price = 21440.0
        fill_ts = "2026-05-15T20:59:00Z"
        entry_snapshot = {
            "side": "BUY",
            "entry_price": 21450.0,
            "qty": 1,
            "bracket_stop": 21430.0,
            "bracket_target": 21490.0,
            "signal_id": "sig-x",
        }

    def _fake_submit_exit(*, bot, bar):  # noqa: ANN001
        submit_exit_called.append({"bot_id": bot.bot_id, "bar": bar})
        bot.open_position = None
        bot.realized_pnl += _FakeFill.realized_pnl
        return _FakeFill()

    monkeypatch.setattr(sup._router, "submit_exit", _fake_submit_exit)
    monkeypatch.setattr(
        sup,
        "_propagate_close",
        lambda bot, rec, entry_snapshot=None: propagated.append(rec),
    )

    bar = {"close": 21440.0, "high": 21442.0, "low": 21438.0, "open": 21440.0}
    fake_now = _ct_to_utc(2026, 5, 15, 15, 59)
    sup._maybe_flatten_for_eod(bot, bar, now=fake_now)

    assert submit_exit_called and submit_exit_called[0]["bot_id"] == "test_bot"
    assert propagated, "propagate_close should have been invoked"
    assert bot.open_position is None
    # The flatten path also flagged the planned exit reason on the position
    # before clearing — but submit_exit clears bot.open_position, so we
    # verify against the call shape rather than the post-state.


def test_daily_loss_cap_halts_new_entries(monkeypatch) -> None:
    """Bot with -4% realized PnL on a 3% cap → _maybe_enter must short-circuit
    before the dice roll / JARVIS consult even when RTH is open."""
    sup, bot = _make_supervisor_with_gated_bot(daily_cap=3.0)
    # Pre-seed session anchor at $0 for today, so the cumulative -$200
    # below counts as session loss against the cap (rather than rolling
    # the anchor to -200 on first call and reading session_pnl=0).
    fake_now = _ct_to_utc(2026, 5, 15, 10, 0)
    bot.session_state.daily_session_date = current_session_date(fake_now)
    bot.session_state.daily_pnl_anchor = 0.0
    # Force the bot to realized_pnl deeply negative (-$200 vs $5k = -4%)
    bot.realized_pnl = -200.0
    monkeypatch.setattr("random.random", lambda: 0.0)

    submit_called: list[bool] = []
    monkeypatch.setattr(
        sup._router,
        "submit_entry",
        lambda **kw: submit_called.append(True) or None,
    )
    consult_called: list[bool] = []
    monkeypatch.setattr(
        sup,
        "_consult_jarvis",
        lambda **kw: consult_called.append(True) or None,
    )

    monkeypatch.setattr(
        "eta_engine.scripts.jarvis_strategy_supervisor.datetime",
        _FakeDatetime(fake_now),
    )
    bar = {"close": 21450.0, "high": 21455.0, "low": 21445.0, "open": 21450.0}
    sup._maybe_enter(bot, bar)

    assert submit_called == []
    assert consult_called == []
    # State recorded the halt
    assert bot.session_state is not None
    assert bot.session_state.halted_until_session_date != ""


def test_daily_loss_cap_does_not_halt_when_below_floor(monkeypatch) -> None:
    """Same bot, smaller loss (-$100 = -2% < 3%) → not halted; entry path
    proceeds through the dice / JARVIS layers."""
    sup, bot = _make_supervisor_with_gated_bot(daily_cap=3.0)
    monkeypatch.setattr(
        "eta_engine.feeds.capital_allocator.resolve_execution_target",
        lambda bot_id, prospective_loss_usd: ("live", "test_live"),
    )
    fake_now = _ct_to_utc(2026, 5, 15, 10, 0)
    bot.session_state.daily_session_date = current_session_date(fake_now)
    bot.session_state.daily_pnl_anchor = 0.0
    bot.realized_pnl = -100.0  # -2%
    monkeypatch.setattr("random.random", lambda: 0.0)

    consult_called: list[bool] = []
    monkeypatch.setattr(
        sup,
        "_consult_jarvis",
        lambda **kw: consult_called.append(True) or None,
    )
    monkeypatch.setattr(sup._router, "submit_entry", lambda **kw: None)
    monkeypatch.setattr(sup, "_consult_sage_for_bot", lambda *a, **kw: None)
    monkeypatch.setattr(sup, "_check_signal_aggregation", lambda **kw: "")
    monkeypatch.setattr(
        "eta_engine.scripts.daily_loss_killswitch.is_killswitch_tripped",
        lambda: (False, ""),
        raising=False,
    )

    monkeypatch.setattr(
        "eta_engine.scripts.jarvis_strategy_supervisor.datetime",
        _FakeDatetime(fake_now),
    )
    bar = {"close": 21450.0, "high": 21455.0, "low": 21445.0, "open": 21450.0}
    sup._maybe_enter(bot, bar)

    # JARVIS was consulted → the daily-loss gate did NOT short-circuit.
    assert consult_called == [True]
    assert bot.session_state is not None
    assert bot.session_state.halted_until_session_date == ""


# ----------------------------------------------------------------------- #
# load_bots integration: registry extras propagate to BotInstance
# ----------------------------------------------------------------------- #


def test_load_bots_propagates_registry_extras(monkeypatch) -> None:
    """Verify the supervisor's load_bots() pulls daily_loss_limit_pct from
    a bot's extras and builds a SessionGate when enable_session_gate=True.

    Uses mbt_funding_basis because its extras have both
    ``edge_config={'enable_session_gate': True, ...}`` and
    ``daily_loss_limit_pct=3.0`` per registry as of 2026-05-07. Skips if
    that bot isn't active in the registry build (so this test is
    self-healing if the registry rotates).
    """
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.data_feed = "mock"
    cfg.bots_env = "mbt_funding_basis"
    sup = JarvisStrategySupervisor(cfg=cfg)
    n = sup.load_bots()
    if n == 0:
        pytest.skip("mbt_funding_basis is not active in this registry build")
    bot = sup.bots[0]
    assert bot.bot_id == "mbt_funding_basis"
    assert bot.session_state is not None
    # enable_session_gate=True → a real gate is built
    assert bot.session_state.gate is not None
    # daily_loss_limit_pct=3.0 should have been pulled from extras
    assert bot.daily_loss_limit_pct == pytest.approx(3.0)


# ----------------------------------------------------------------------- #
# Datetime stub used by the supervisor tests above
# ----------------------------------------------------------------------- #


class _FakeDatetime:
    """Minimal datetime replacement that pins ``now(tz)`` to a fixed value
    while passing every other attribute through to the real class.
    """

    def __init__(self, fixed_now: datetime) -> None:
        self._now = fixed_now

    def now(self, tz: Any = None) -> datetime:  # noqa: ARG002
        return self._now

    def __getattr__(self, name: str) -> Any:
        return getattr(datetime, name)

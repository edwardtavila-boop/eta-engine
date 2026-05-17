from __future__ import annotations

import logging
from types import SimpleNamespace

from eta_engine.scripts import supervisor_entry_helpers


def test_build_entry_fill_record_payload_rounds_and_formats() -> None:
    payload = supervisor_entry_helpers.build_entry_fill_record_payload(
        bot_id="btc_entry",
        signal_id="sig-entry",
        side="BUY",
        symbol="BTC",
        qty=0.125,
        fill_price=81234.56789,
        fill_ts="2026-05-17T20:00:00+00:00",
        mode="paper_live",
    )

    assert payload["fill_price"] == 81234.5679
    assert payload["paper"] is True
    assert payload["note"] == "mode=paper_live"


def test_record_optimistic_entry_persists_bracket_and_initial_risk() -> None:
    persisted: list[dict[str, object]] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        symbol="MNQ1",
        sage_bars=[{"close": 100.0}],
        open_position=None,
    )
    rec = SimpleNamespace(
        side="BUY",
        qty=2.0,
        fill_price=100.25,
        fill_ts="2026-05-17T20:01:00+00:00",
        signal_id="sig-entry",
    )

    warned = supervisor_entry_helpers.record_optimistic_entry(
        bot=bot,
        rec=rec,
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        persist_open_position_fn=lambda current_bot: persisted.append(dict(current_bot.open_position)),
        round_to_tick_fn=lambda price, _symbol: round(price, 2),
        warned_bots=None,
        compute_bracket_fn=lambda **_kwargs: (95.0, 110.0, "atr"),
        lookup_bot_bracket_params_fn=lambda _bot_id: (1.5, 2.5),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert warned == set()
    assert bot.open_position is not None
    assert bot.open_position["signal_id"] == "sig-entry"
    assert bot.open_position["bracket_stop"] == 95.0
    assert bot.open_position["bracket_target"] == 110.0
    assert bot.open_position["bracket_src"] == "paper:atr"
    assert bot.open_position["initial_stop_distance"] == 5.25
    assert bot.open_position["initial_risk_unit"] == 21.0
    assert len(persisted) == 2


def test_record_optimistic_entry_uses_absolute_qty_for_initial_risk() -> None:
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        symbol="MNQ1",
        sage_bars=[{"close": 100.0}],
        open_position=None,
    )
    rec = SimpleNamespace(
        side="SELL",
        qty=-2.0,
        fill_price=100.25,
        fill_ts="2026-05-17T20:01:00+00:00",
        signal_id="sig-entry",
    )

    supervisor_entry_helpers.record_optimistic_entry(
        bot=bot,
        rec=rec,
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        persist_open_position_fn=lambda _bot: None,
        round_to_tick_fn=lambda price, _symbol: round(price, 2),
        compute_bracket_fn=lambda **_kwargs: (105.0, 90.0, "atr"),
        lookup_bot_bracket_params_fn=lambda _bot_id: (1.5, 2.5),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert bot.open_position is not None
    assert bot.open_position["initial_risk_unit"] == 19.0


def test_apply_entry_accounting_updates_bot_state() -> None:
    bot = SimpleNamespace(n_entries=2, last_signal_at="")

    supervisor_entry_helpers.apply_entry_accounting(
        bot,
        fill_ts="2026-05-17T20:02:00+00:00",
    )

    assert bot.n_entries == 3
    assert bot.last_signal_at == "2026-05-17T20:02:00+00:00"


def test_paper_live_direct_crypto_bypasses_broker_when_disabled() -> None:
    assert (
        supervisor_entry_helpers.paper_live_direct_crypto_bypasses_broker(
            "BTC",
            crypto_live_env="",
        )
        is True
    )
    assert (
        supervisor_entry_helpers.paper_live_direct_crypto_bypasses_broker(
            "MNQ1",
            crypto_live_env="",
        )
        is False
    )
    assert (
        supervisor_entry_helpers.paper_live_direct_crypto_bypasses_broker(
            "ETH",
            crypto_live_env="true",
        )
        is False
    )


def test_build_direct_ibkr_entry_plan_builds_market_request() -> None:
    class _Request:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    bot = SimpleNamespace(
        bot_id="mnq_entry",
        symbol="MNQ1",
        sage_bars=[{"close": 100.0}],
    )
    rec = SimpleNamespace(
        side="BUY",
        qty=2.0,
        fill_price=100.25,
        signal_id="sig-entry",
        symbol="MNQ1",
    )

    plan = supervisor_entry_helpers.build_direct_ibkr_entry_plan(
        bot=bot,
        rec=rec,
        bar={"close": 100.0},
        round_to_tick_fn=lambda price, _symbol: round(price, 2),
        compute_bracket_fn=lambda **_kwargs: (95.0, 110.0, "atr"),
        lookup_bot_bracket_params_fn=lambda _bot_id: (1.5, 2.5),
        order_request_cls=_Request,
        order_type_market="MARKET",
        side_buy="BUY_SIDE",
        side_sell="SELL_SIDE",
    )

    assert plan.ref_price == 100.25
    assert plan.stop_price == 95.0
    assert plan.target_price == 110.0
    assert plan.bracket_src == "atr"
    assert plan.request.symbol == "MNQ1"
    assert plan.request.side == "BUY_SIDE"
    assert plan.request.order_type == "MARKET"
    assert plan.request.price == 100.25
    assert plan.request.stop_price == 95.0
    assert plan.request.target_price == 110.0


def test_build_direct_ibkr_entry_plan_rejects_invalid_geometry() -> None:
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        symbol="MNQ1",
        sage_bars=[{"close": 100.0}],
    )
    rec = SimpleNamespace(
        side="BUY",
        qty=1.0,
        fill_price=100.25,
        signal_id="sig-entry",
        symbol="MNQ1",
    )

    try:
        supervisor_entry_helpers.build_direct_ibkr_entry_plan(
            bot=bot,
            rec=rec,
            bar={"close": 100.0},
            round_to_tick_fn=lambda price, _symbol: round(price, 2),
            compute_bracket_fn=lambda **_kwargs: (101.0, 99.0, "broken"),
            lookup_bot_bracket_params_fn=lambda _bot_id: (1.5, 2.5),
            order_request_cls=dict,
            order_type_market="MARKET",
            side_buy="BUY_SIDE",
            side_sell="SELL_SIDE",
        )
    except ValueError as exc:
        assert "insane bracket geometry" in str(exc)
    else:
        raise AssertionError("expected invalid geometry to raise ValueError")


def test_direct_ibkr_result_reason_prefers_reason_then_dedup_note() -> None:
    explicit = SimpleNamespace(raw={"reason": "paper_blocked"})
    deduped = SimpleNamespace(raw={"deduped": True, "note": "already working"})
    unknown = SimpleNamespace(raw={})

    assert supervisor_entry_helpers.direct_ibkr_result_reason(explicit) == "paper_blocked"
    assert supervisor_entry_helpers.direct_ibkr_result_reason(deduped) == "deduped: already working"
    assert supervisor_entry_helpers.direct_ibkr_result_reason(unknown) == "n/a"


def test_finalize_direct_ibkr_entry_result_marks_filled_position_and_records_fill() -> None:
    record_signal_calls: list[tuple[object, object, object]] = []
    record_fill_calls: list[dict[str, object]] = []
    rollback_calls: list[str] = []
    clear_calls: list[str] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        open_position={"qty": 3.0},
        consecutive_broker_rejects=4,
    )
    rec = SimpleNamespace(
        signal_id="sig-entry",
        side="BUY",
        note="mode=paper_live",
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="FILLED"),
        filled_qty=2.0,
        avg_price=100.5,
        fees=1.25,
        order_id="ibkr-1",
        raw={"ibkr_order_id": 1234},
    )
    plan = supervisor_entry_helpers.DirectIbkrEntryPlan(
        request=object(),
        ref_price=100.25,
        stop_price=95.0,
        target_price=110.0,
        bracket_src="atr",
    )

    outcome = supervisor_entry_helpers.finalize_direct_ibkr_entry_result(
        bot=bot,
        rec=rec,
        result=result,
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        entry_plan=plan,
        record_signal_fn=lambda *args: record_signal_calls.append(args),
        record_fill_fn=lambda **kwargs: record_fill_calls.append(kwargs),
        rollback_recorded_entry_fn=rollback_calls.append,
        clear_recorded_entry_without_reject_fn=clear_calls.append,
    )

    assert outcome.action == "filled"
    assert outcome.reason == "n/a"
    assert outcome.filled_qty == 2.0
    assert bot.open_position["qty"] == 2.0
    assert bot.open_position["broker_bracket"] is True
    assert bot.open_position["bracket_stop"] == 95.0
    assert bot.open_position["bracket_target"] == 110.0
    assert bot.open_position["bracket_src"] == "atr"
    assert bot.consecutive_broker_rejects == 0
    assert len(record_signal_calls) == 1
    assert record_fill_calls[0]["broker_exec_id"] == "1234"
    assert record_fill_calls[0]["actual_fill_price"] == 100.5
    assert rollback_calls == []
    assert clear_calls == []


def test_finalize_direct_ibkr_entry_result_clears_pending_without_reject() -> None:
    clear_calls: list[str] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        open_position={"qty": 1.0},
        consecutive_broker_rejects=2,
    )
    rec = SimpleNamespace(
        signal_id="sig-entry",
        side="BUY",
        note="mode=paper_live",
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="OPEN"),
        filled_qty=0.0,
        raw={"reason": "working"},
    )
    plan = supervisor_entry_helpers.DirectIbkrEntryPlan(
        request=object(),
        ref_price=100.25,
        stop_price=95.0,
        target_price=110.0,
        bracket_src="atr",
    )

    outcome = supervisor_entry_helpers.finalize_direct_ibkr_entry_result(
        bot=bot,
        rec=rec,
        result=result,
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        entry_plan=plan,
        record_signal_fn=lambda *_args: None,
        record_fill_fn=lambda **_kwargs: None,
        rollback_recorded_entry_fn=lambda _reason: None,
        clear_recorded_entry_without_reject_fn=clear_calls.append,
    )

    assert outcome.action == "pending"
    assert outcome.reason == "working"
    assert rec.note.endswith("direct_ibkr_pending_order")
    assert clear_calls == ["direct_ibkr_open_without_fill"]


def test_finalize_direct_ibkr_entry_result_fails_open_on_l2_callback_exceptions() -> None:
    warnings: list[tuple[object, ...]] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        open_position={"qty": 1.0},
        consecutive_broker_rejects=2,
    )
    rec = SimpleNamespace(
        signal_id="sig-entry",
        side="BUY",
        note="mode=paper_live",
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="FILLED"),
        filled_qty=1.0,
        avg_price=100.5,
        fees=1.25,
        order_id="ibkr-1",
        raw={"ibkr_order_id": 1234},
    )
    logger = SimpleNamespace(warning=lambda *args: warnings.append(args))
    plan = supervisor_entry_helpers.DirectIbkrEntryPlan(
        request=object(),
        ref_price=100.25,
        stop_price=95.0,
        target_price=110.0,
        bracket_src="atr",
    )

    outcome = supervisor_entry_helpers.finalize_direct_ibkr_entry_result(
        bot=bot,
        rec=rec,
        result=result,
        logger=logger,  # type: ignore[arg-type]
        entry_plan=plan,
        record_signal_fn=lambda *_args: (_ for _ in ()).throw(RuntimeError("signal_down")),
        record_fill_fn=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fill_down")),
        rollback_recorded_entry_fn=lambda _reason: (_ for _ in ()).throw(AssertionError("unexpected rollback")),
        clear_recorded_entry_without_reject_fn=(
            lambda _reason: (_ for _ in ()).throw(AssertionError("unexpected clear"))
        ),
    )

    assert outcome.action == "filled"
    assert bot.open_position["broker_bracket"] is True
    assert bot.consecutive_broker_rejects == 0
    assert len(warnings) == 2
    assert warnings[0][0] == "l2 record_signal failed for %s: %s"
    assert warnings[1][0] == "l2 record_fill failed for %s: %s"


def test_finalize_direct_ibkr_entry_result_rolls_back_rejected_status() -> None:
    rollback_calls: list[str] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        open_position={"qty": 1.0},
        consecutive_broker_rejects=0,
    )
    rec = SimpleNamespace(
        signal_id="sig-entry",
        side="BUY",
        note="mode=paper_live",
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="REJECTED"),
        filled_qty=0.0,
        raw={"reason": "ibkr_reject"},
    )
    plan = supervisor_entry_helpers.DirectIbkrEntryPlan(
        request=object(),
        ref_price=100.25,
        stop_price=95.0,
        target_price=110.0,
        bracket_src="atr",
    )

    outcome = supervisor_entry_helpers.finalize_direct_ibkr_entry_result(
        bot=bot,
        rec=rec,
        result=result,
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        entry_plan=plan,
        record_signal_fn=lambda *_args: None,
        record_fill_fn=lambda **_kwargs: None,
        rollback_recorded_entry_fn=rollback_calls.append,
        clear_recorded_entry_without_reject_fn=(
            lambda _reason: (_ for _ in ()).throw(AssertionError("unexpected clear"))
        ),
    )

    assert outcome.action == "rejected"
    assert outcome.reason == "ibkr_reject"
    assert rollback_calls == ["broker_result=REJECTED; filled_qty=0.0; reason=ibkr_reject"]


def test_rollback_recorded_entry_clears_state_and_counts_reject() -> None:
    cleared: list[object] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        open_position={"signal_id": "sig-entry"},
        n_entries=1,
        consecutive_broker_rejects=0,
    )
    rec = SimpleNamespace(
        signal_id="sig-entry",
        symbol="MNQ1",
        side="BUY",
        qty=1.0,
    )

    supervisor_entry_helpers.rollback_recorded_entry(
        bot=bot,
        rec=rec,
        reason="venue_reject",
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        clear_persisted_open_position_fn=lambda current_bot: cleared.append(current_bot),
    )

    assert bot.open_position is None
    assert bot.n_entries == 0
    assert bot.consecutive_broker_rejects == 1
    assert cleared == [bot]


def test_clear_recorded_entry_without_reject_clears_state_without_incrementing_rejects() -> None:
    cleared: list[object] = []
    bot = SimpleNamespace(
        bot_id="mnq_entry",
        open_position={"signal_id": "sig-entry"},
        n_entries=1,
        consecutive_broker_rejects=2,
    )
    rec = SimpleNamespace(
        signal_id="sig-entry",
        symbol="MNQ1",
        side="BUY",
        qty=1.0,
    )

    supervisor_entry_helpers.clear_recorded_entry_without_reject(
        bot=bot,
        rec=rec,
        reason="pending_file",
        logger=logging.getLogger("test_supervisor_entry_helpers"),
        clear_persisted_open_position_fn=lambda current_bot: cleared.append(current_bot),
    )

    assert bot.open_position is None
    assert bot.n_entries == 0
    assert bot.consecutive_broker_rejects == 2
    assert cleared == [bot]

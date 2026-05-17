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

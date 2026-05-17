from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import pytest

from eta_engine.scripts import supervisor_exit_helpers


def test_reconcile_exit_qty_prefers_smaller_broker_qty() -> None:
    decision = supervisor_exit_helpers.reconcile_exit_qty(
        {"qty": 2.0},
        1.25,
        bot_id="mnq_exit",
        logger=logging.getLogger("test_supervisor_exit_helpers"),
    )

    assert decision.supervisor_qty == 2.0
    assert decision.broker_qty == 1.25
    assert decision.exit_qty == 1.25


def test_reconcile_exit_qty_uses_supervisor_qty_when_broker_unavailable() -> None:
    decision = supervisor_exit_helpers.reconcile_exit_qty(
        {"qty": 0.5},
        None,
        bot_id="btc_exit",
        logger=logging.getLogger("test_supervisor_exit_helpers"),
    )

    assert decision.supervisor_qty == 0.5
    assert decision.broker_qty is None
    assert decision.exit_qty == 0.5


def test_reconcile_exit_qty_normalizes_signed_broker_qty() -> None:
    decision = supervisor_exit_helpers.reconcile_exit_qty(
        {"qty": 1.0},
        -0.5,
        bot_id="short_broker_qty",
        logger=logging.getLogger("test_supervisor_exit_helpers"),
    )

    assert decision.supervisor_qty == 1.0
    assert decision.broker_qty == 0.5
    assert decision.exit_qty == 0.5


def test_reconcile_exit_qty_treats_non_finite_broker_qty_as_unavailable() -> None:
    decision = supervisor_exit_helpers.reconcile_exit_qty(
        {"qty": 1.0},
        math.nan,
        bot_id="nan_broker_qty",
        logger=logging.getLogger("test_supervisor_exit_helpers"),
    )

    assert decision.supervisor_qty == 1.0
    assert decision.broker_qty is None
    assert decision.exit_qty == 1.0


def test_compute_paper_exit_fill_price_uses_bracket_stop_for_long_exit() -> None:
    fill_price = supervisor_exit_helpers.compute_paper_exit_fill_price(
        {
            "side": "BUY",
            "entry_price": 100.0,
            "exit_reason": "paper_stop",
            "bracket_stop": 95.0,
        },
        {"close": 102.0},
        symbol="BTC",
        adverse_bps=1.5,
        round_to_tick_fn=lambda price, _symbol: round(price, 6),
    )

    assert fill_price < 95.0


def test_compute_paper_exit_fill_price_normalizes_lowercase_side() -> None:
    fill_price = supervisor_exit_helpers.compute_paper_exit_fill_price(
        {
            "side": "buy",
            "entry_price": 100.0,
            "exit_reason": "paper_stop",
            "bracket_stop": 95.0,
        },
        {"close": 102.0},
        symbol="BTC",
        adverse_bps=1.5,
        round_to_tick_fn=lambda price, _symbol: round(price, 6),
    )

    assert fill_price < 95.0


def test_compute_paper_exit_fill_price_strips_side_whitespace() -> None:
    fill_price = supervisor_exit_helpers.compute_paper_exit_fill_price(
        {
            "side": " buy ",
            "entry_price": 100.0,
            "exit_reason": "paper_stop",
            "bracket_stop": 95.0,
        },
        {"close": 102.0},
        symbol="BTC",
        adverse_bps=1.5,
        round_to_tick_fn=lambda price, _symbol: round(price, 6),
    )

    assert fill_price < 95.0


def test_compute_paper_exit_fill_price_accepts_long_side_alias() -> None:
    fill_price = supervisor_exit_helpers.compute_paper_exit_fill_price(
        {
            "side": " long ",
            "entry_price": 100.0,
            "exit_reason": "paper_stop",
            "bracket_stop": 95.0,
        },
        {"close": 102.0},
        symbol="BTC",
        adverse_bps=1.5,
        round_to_tick_fn=lambda price, _symbol: round(price, 6),
    )

    assert fill_price < 95.0


def test_compute_paper_exit_fill_price_rejects_unknown_side() -> None:
    with pytest.raises(ValueError, match="unknown exit side"):
        supervisor_exit_helpers.compute_paper_exit_fill_price(
            {
                "side": "hold",
                "entry_price": 100.0,
            },
            {"close": 102.0},
            symbol="BTC",
            adverse_bps=1.5,
            round_to_tick_fn=lambda price, _symbol: round(price, 6),
        )


def test_compute_paper_exit_fill_price_uses_target_without_slippage() -> None:
    fill_price = supervisor_exit_helpers.compute_paper_exit_fill_price(
        {
            "side": "SELL",
            "entry_price": 100.0,
            "exit_reason": "paper_target",
            "bracket_target": 92.5,
        },
        {"close": 91.0},
        symbol="ETH",
        adverse_bps=1.5,
        round_to_tick_fn=lambda price, _symbol: round(price, 6),
    )

    assert fill_price == 92.5


def test_build_entry_snapshot_preserves_latency_fields() -> None:
    snapshot = supervisor_exit_helpers.build_entry_snapshot(
        {
            "side": "BUY",
            "entry_price": 100.0,
            "qty": 1.0,
            "signal_id": "sig-1",
            "entry_fill_age_s": 42.0,
            "entry_fill_latency_source": "broker_router_fill",
            "entry_fill_age_precision": "fill_to_adopt",
            "broker_fill_ts": "2026-05-17T18:00:00+00:00",
            "broker_router_result_ts": "2026-05-17T18:00:02+00:00",
            "fill_to_adopt_delay_s": 1.5,
            "fill_result_write_delay_s": 0.5,
        }
    )

    assert snapshot["entry_fill_age_s"] == 42.0
    assert snapshot["entry_fill_latency_source"] == "broker_router_fill"
    assert snapshot["entry_fill_age_precision"] == "fill_to_adopt"
    assert snapshot["broker_fill_ts"] == "2026-05-17T18:00:00+00:00"


def test_compute_exit_realization_prefers_initial_risk_unit() -> None:
    valuation = supervisor_exit_helpers.compute_exit_realization(
        {
            "side": "BUY",
            "entry_price": 100.0,
            "initial_risk_unit": 25.0,
            "bracket_stop": 95.0,
        },
        symbol="MNQ1",
        fill_price=110.0,
        exit_qty=2.0,
        cash=5_000.0,
        logger=logging.getLogger("test_supervisor_exit_helpers"),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert valuation.point_value == 2.0
    assert valuation.pnl == 40.0
    assert valuation.realized_r == 1.6


def test_compute_exit_realization_falls_back_to_cash_risk_when_stop_missing() -> None:
    valuation = supervisor_exit_helpers.compute_exit_realization(
        {
            "side": "SELL",
            "entry_price": 100.0,
        },
        symbol="ETH",
        fill_price=90.0,
        exit_qty=1.0,
        cash=2_000.0,
        logger=logging.getLogger("test_supervisor_exit_helpers"),
        point_value_fn=lambda _symbol, _route: 1.0,
    )

    assert valuation.point_value == 1.0
    assert valuation.pnl == 10.0
    assert valuation.realized_r == 0.5


def test_compute_exit_realization_accepts_numeric_string_position_fields() -> None:
    valuation = supervisor_exit_helpers.compute_exit_realization(
        {
            "side": "BUY",
            "entry_price": "100.0",
            "bracket_stop": "95.0",
        },
        symbol="MNQ1",
        fill_price=110.0,
        exit_qty=2.0,
        cash=5_000.0,
        logger=logging.getLogger("test_supervisor_exit_helpers"),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert valuation.point_value == 2.0
    assert valuation.pnl == 40.0
    assert valuation.realized_r == 2.0


def test_compute_exit_realization_normalizes_lowercase_side() -> None:
    valuation = supervisor_exit_helpers.compute_exit_realization(
        {
            "side": "buy",
            "entry_price": 100.0,
            "bracket_stop": 95.0,
        },
        symbol="MNQ1",
        fill_price=110.0,
        exit_qty=2.0,
        cash=5_000.0,
        logger=logging.getLogger("test_supervisor_exit_helpers"),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert valuation.pnl == 40.0
    assert valuation.realized_r == 2.0


def test_compute_exit_realization_strips_side_whitespace() -> None:
    valuation = supervisor_exit_helpers.compute_exit_realization(
        {
            "side": " buy ",
            "entry_price": 100.0,
            "bracket_stop": 95.0,
        },
        symbol="MNQ1",
        fill_price=110.0,
        exit_qty=2.0,
        cash=5_000.0,
        logger=logging.getLogger("test_supervisor_exit_helpers"),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert valuation.pnl == 40.0
    assert valuation.realized_r == 2.0


def test_compute_exit_realization_accepts_long_side_alias() -> None:
    valuation = supervisor_exit_helpers.compute_exit_realization(
        {
            "side": " long ",
            "entry_price": 100.0,
            "bracket_stop": 95.0,
        },
        symbol="MNQ1",
        fill_price=110.0,
        exit_qty=2.0,
        cash=5_000.0,
        logger=logging.getLogger("test_supervisor_exit_helpers"),
        point_value_fn=lambda _symbol, _route: 2.0,
    )

    assert valuation.pnl == 40.0
    assert valuation.realized_r == 2.0


def test_compute_exit_realization_rejects_unknown_side() -> None:
    with pytest.raises(ValueError, match="unknown exit side"):
        supervisor_exit_helpers.compute_exit_realization(
            {
                "side": "hold",
                "entry_price": 100.0,
            },
            symbol="MNQ1",
            fill_price=110.0,
            exit_qty=2.0,
            cash=5_000.0,
            logger=logging.getLogger("test_supervisor_exit_helpers"),
            point_value_fn=lambda _symbol, _route: 2.0,
        )


def test_build_exit_fill_record_payload_rounds_and_formats() -> None:
    payload = supervisor_exit_helpers.build_exit_fill_record_payload(
        bot_id="btc_bot",
        signal_id="sig-exit",
        side="SELL",
        symbol="BTC",
        qty=0.125,
        fill_price=81234.56789,
        fill_ts="2026-05-17T19:30:00+00:00",
        realized_r=1.23456,
        pnl=123.456,
    )

    assert payload["fill_price"] == 81234.5679
    assert payload["realized_r"] == 1.2346
    assert payload["realized_pnl"] == 123.456
    assert payload["note"] == "close pnl=+123.46"


def test_apply_exit_accounting_updates_bot_state() -> None:
    bot = SimpleNamespace(realized_pnl=10.0, cash=1000.0, n_exits=2)

    supervisor_exit_helpers.apply_exit_accounting(bot, pnl=25.5)

    assert bot.realized_pnl == 35.5
    assert bot.cash == 1025.5
    assert bot.n_exits == 3


def test_maybe_route_paper_live_exit_emits_reduce_only() -> None:
    calls: list[tuple[object, object, bool]] = []

    def _writer(bot, rec, *, reduce_only: bool) -> None:
        calls.append((bot, rec, reduce_only))

    bot = object()
    rec = object()

    supervisor_exit_helpers.maybe_route_paper_live_exit(
        mode="paper_live",
        write_pending_order_fn=_writer,
        bot=bot,
        rec=rec,
    )
    supervisor_exit_helpers.maybe_route_paper_live_exit(
        mode="paper_sim",
        write_pending_order_fn=_writer,
        bot=bot,
        rec=rec,
    )

    assert calls == [(bot, rec, True)]


def test_record_cross_bot_exit_records_normalized_symbol() -> None:
    calls: list[dict[str, object]] = []

    class _Tracker:
        def record_exit(self, *, symbol_root: str, side: str, qty: float) -> None:
            calls.append({"symbol_root": symbol_root, "side": side, "qty": qty})

    logger = logging.getLogger("test_supervisor_exit_helpers")
    tracker = _Tracker()

    supervisor_exit_helpers.record_cross_bot_exit(
        bot_id="mnq_exit",
        symbol="MNQ1",
        side="SELL",
        qty=1.0,
        logger=logger,
        get_tracker_fn=lambda: tracker,
        normalize_root_fn=lambda symbol: symbol.rstrip("1"),
    )

    assert calls == [{"symbol_root": "MNQ", "side": "SELL", "qty": 1.0}]


def test_record_cross_bot_exit_swallows_failures() -> None:
    warnings: list[tuple[object, ...]] = []

    class _Logger:
        def warning(self, *args) -> None:
            warnings.append(args)

    supervisor_exit_helpers.record_cross_bot_exit(
        bot_id="btc_exit",
        symbol="BTC",
        side="BUY",
        qty=0.25,
        logger=_Logger(),  # type: ignore[arg-type]
        get_tracker_fn=lambda: (_ for _ in ()).throw(RuntimeError("tracker_down")),
        normalize_root_fn=lambda symbol: symbol,
    )

    assert warnings
    assert warnings[0][0] == "cross_bot_tracker.record_exit(%s) failed: %s"
    assert warnings[0][1] == "btc_exit"


def test_clear_exit_position_state_clears_bot_and_persistence() -> None:
    cleared: list[object] = []
    bot = SimpleNamespace(open_position={"side": "BUY"})

    supervisor_exit_helpers.clear_exit_position_state(
        bot=bot,
        clear_persisted_open_position_fn=lambda current_bot: cleared.append(current_bot),
    )

    assert bot.open_position is None
    assert cleared == [bot]

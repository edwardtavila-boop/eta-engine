from __future__ import annotations

import asyncio

from eta_engine.scripts.btc_paper_trade import (
    AlwaysApproveGate,
    BtcPaperRunner,
    ConfluenceFloorGate,
    PaperRouter,
    _assess_verdict,
    _cli_parse,
    synthetic_btc_stream,
)
from eta_engine.scripts import workspace_roots
from eta_engine.venues.base import OrderRequest, OrderStatus, OrderType, Side


def test_paper_router_fills_at_last_close_when_price_missing() -> None:
    router = PaperRouter(fee_bps=2.0)
    router.mark_bar({"close": 61_250.0})

    result = asyncio.run(
        router.place_with_failover(
            OrderRequest(
                symbol="BTCUSD",
                side=Side.BUY,
                qty=0.25,
                order_type=OrderType.MARKET,
            )
        )
    )

    assert result.status is OrderStatus.FILLED
    assert result.avg_price == 61_250.0
    assert len(router.fills) == 1
    assert router.fills[0].price == 61_250.0


def test_paper_router_rejects_without_price_or_marked_bar() -> None:
    router = PaperRouter()

    result = asyncio.run(
        router.place_with_failover(
            OrderRequest(
                symbol="BTCUSD",
                side=Side.BUY,
                qty=0.25,
                order_type=OrderType.MARKET,
            )
        )
    )

    assert result.status is OrderStatus.REJECTED
    assert result.raw["reason"] == "no-last-close"
    assert router.fills == []


def test_confluence_floor_gate_blocks_below_floor_and_allows_above() -> None:
    gate = ConfluenceFloorGate(7.0)

    blocked, blocked_reason = gate.decide({"confidence": 6.5})
    allowed, allowed_reason = gate.decide({"confidence": 7.5})

    assert blocked is False
    assert "blocked" in blocked_reason
    assert allowed is True
    assert "approved" in allowed_reason


def test_always_approve_gate_includes_confidence_in_reason() -> None:
    approved, reason = AlwaysApproveGate().decide({"confidence": 8.0})

    assert approved is True
    assert "paper-shakedown approve" in reason
    assert "8.0" in reason


def test_assess_verdict_collects_failure_reasons() -> None:
    verdict, reasons = _assess_verdict(
        starting_equity=10_000.0,
        final_equity=8_500.0,
        max_dd_pct=0.12,
        overlay_win_rate=0.0,
        overlay_signals=0,
        kill_triggered=True,
    )

    assert verdict == "FAIL"
    assert any("kill_switch" in reason for reason in reasons)
    assert any("max_dd_pct" in reason for reason in reasons)
    assert any("final_equity" in reason for reason in reasons)
    assert any("overlay signals" in reason for reason in reasons)


def test_synthetic_btc_stream_emits_high_confluence_on_overlay_cadence() -> None:
    async def _collect() -> list[dict[str, float]]:
        rows: list[dict[str, float]] = []
        async for row in synthetic_btc_stream(n_bars=4, overlay_every=2):
            rows.append(row)
        return rows

    rows = asyncio.run(_collect())

    assert len(rows) == 4
    assert rows[0]["confluence_score"] == 8.0
    assert rows[1]["confluence_score"] == 3.0
    assert rows[2]["confluence_score"] == 8.0
    assert rows[3]["confluence_score"] == 3.0
    assert rows[0]["ema_9"] != rows[0]["ema_21"]
    assert rows[2]["ema_9"] != rows[2]["ema_21"]


def test_btc_paper_runner_symbol_is_exposed_from_canonical_module() -> None:
    assert BtcPaperRunner.__name__ == "BtcPaperRunner"


def test_btc_paper_cli_defaults_to_canonical_state_dir() -> None:
    args = _cli_parse([])
    assert args.out_dir == str(workspace_roots.ETA_BTC_PAPER_STATE_DIR)

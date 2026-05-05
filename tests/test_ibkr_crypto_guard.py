"""Regression coverage for IBKR crypto permission pre-checks."""

from __future__ import annotations

from importlib import reload
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_crypto_permission_pre_reject_does_not_poison_idempotency(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETA_LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("ETA_FLEET_RISK_LIMIT", "100000")
    monkeypatch.setenv("ETA_POSITION_CAP", "5")
    monkeypatch.delenv("ETA_IBKR_CRYPTO", raising=False)
    monkeypatch.setenv("ETA_IDEMPOTENCY_STORE", str(tmp_path / "idem.jsonl"))

    from eta_engine.safety import idempotency

    idempotency.reset_store_for_test()
    reload(idempotency)

    import eta_engine.venues.ibkr_live as ibkr_live
    from eta_engine.venues.base import OrderRequest, Side

    async def fake_make_contract(symbol: str, ib: object) -> SimpleNamespace:
        assert symbol == "BTC"
        assert ib is not None
        return SimpleNamespace(secType="CRYPTO", symbol="BTC")

    monkeypatch.setattr(ibkr_live, "_make_contract", fake_make_contract)
    ibkr_live._reset_crypto_guard_log_latch()

    venue = ibkr_live.LiveIbkrVenue()
    venue._ensure_connected = AsyncMock(return_value=True)
    venue._ib = object()

    result = await venue.place_order(
        OrderRequest(
            symbol="BTC",
            side=Side.BUY,
            qty=0.01,
            client_order_id="crypto-no-cache-1",
        )
    )

    assert result.status.value == "REJECTED"
    assert result.raw["reason_code"] == "crypto_disabled"
    assert result.raw["no_cache"] is True

    retry = idempotency.check_or_register(
        client_order_id="crypto-no-cache-1",
        venue="ibkr",
        symbol="BTC",
        intent_payload={"side": "BUY", "qty": 0.01},
    )
    assert retry.is_new
